"""微博固定旧实现与根适配器在相同离线边界上的请求、产物和事件对照。"""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image
import pytest
from playwright.async_api import Error as PlaywrightError
from tenacity import RetryError

from support.weibo_adapter import ROOT, load_baseline, settings
from trippostcollect.application import events
from trippostcollect.artifacts.jsonl import AsyncFileWriter, JsonlContentStore
from trippostcollect.platforms import entry
from trippostcollect.platforms.weibo import client, core
from trippostcollect.runtime import image_retry


def mblog(note_id, *, long=False):
    return {
        "id": note_id, "text": "截断…全文" if long else "青岛完整正文",
        "isLongText": long, "created_at": "Sat Jun 14 12:00:00 +0800 2025",
        "attitudes_count": 1, "comments_count": 2, "reposts_count": 3,
        "pics": [{"pid": f"p-{note_id}", "url": f"https://wx1.sinaimg.cn/orj360/{note_id}.jpg"}],
        "user": {"id": "author", "screen_name": "青岛作者", "followers_count": 0,
                 "avatar_hd": "https://avatar.invalid/never.jpg"},
    }


def exception_chain(exc):
    result = []
    while exc is not None:
        # RetryError 的 repr 含 Future 内存地址；比较实际终次异常与尝试次数。
        detail = (exc.last_attempt.attempt_number, type(exc.last_attempt.exception()).__name__, str(exc.last_attempt.exception())) if isinstance(exc, RetryError) else str(exc)
        result.append((type(exc).__name__, detail, getattr(exc, "code", None)))
        exc = exc.__cause__
    return result


async def drive(legacy, directory, monkeypatch, scenario, post_repair):
    """只替换网络、浏览器、行为出口和时钟；解析、重试与暂存保留真实实现。"""
    directory.mkdir()
    trace = []
    counts = Counter()
    data_root = directory / "batch"
    state = directory / "state.json"
    state.write_text('{"events": []}')
    database = directory / "posts.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
        connection.execute("INSERT INTO web_posts VALUES ('weibo', 'known', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(database))
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", str(post_repair))
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS", "7000")
    monkeypatch.setattr(events, "_utc_iso", lambda: "2026-09-30T00:00:00+00:00")
    monkeypatch.setattr(image_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0, 0))
    output = io.BytesIO()
    Image.new("RGB", (4, 3), "blue").save(output, format="PNG")
    image_bytes = output.getvalue()

    async def sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", sleep)

    class Page:
        def __init__(self):
            self.context = context
            self.request = SimpleNamespace(get=self.api)

        async def goto(self, url, **kwargs):
            trace.append(("goto", url, kwargs))
            if scenario == "repair_navigation" and "/detail/" in url:
                raise PlaywrightError("离线导航失败")

        async def wait_for_timeout(self, timeout):
            trace.append(("wait", timeout))

        async def evaluate(self, script, note_id):
            trace.append(("evaluate", script, note_id))
            if scenario == "repair_empty":
                return {"id": "wrong", "text": "其他帖子"}
            return {"state": mblog(note_id)}

        async def api(self, url, **kwargs):
            trace.append(("browser_api", "GET", url, kwargs))
            payload = {"status": mblog("long")} if scenario == "repair_api" else {}

            async def read_json():
                return payload

            status = 429 if scenario == "repair_rate_limit" else 200
            return SimpleNamespace(status=status, json=read_json)

    class Context:
        async def new_page(self):
            trace.append(("new_page",))
            return page

        async def cookies(self, urls=None):
            trace.append(("cookies", urls))
            return [{"name": "SUB", "value": "t05", "domain": ".weibo.cn", "path": "/"}]

        async def add_cookies(self, values):
            trace.append(("add_cookies", values))

        async def add_init_script(self, **kwargs):
            trace.append(("stealth", Path(kwargs["path"]).read_bytes()))

        async def close(self):
            trace.append(("close",))

    context = Context()
    page = Page()

    class Browser:
        async def launch(self, **kwargs):
            trace.append(("launch", kwargs))
            return self

        async def new_context(self, **kwargs):
            trace.append(("new_context", kwargs))
            return context

        async def __aenter__(self):
            return SimpleNamespace(chromium=self)

        async def __aexit__(self, *args):
            trace.append(("playwright_exit",))

    async def behavior(page_arg, platform):
        assert page_arg is page
        trace.append(("behavior", platform))

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            trace.append(("request", method, url, deepcopy(kwargs)))
            parsed = urlparse(url)
            counts[parsed.path] += 1
            status = 200
            payload = None
            content = None
            if parsed.path == "/api/config":
                payload = {"ok": 1, "data": {"login": scenario != "login_expired", "uid": "123"}}
            elif parsed.path == "/api/container/getIndex":
                query = parse_qs(parsed.query)
                number = int(query["page"][0])
                if scenario == "search_retry" and counts[parsed.path] < 3:
                    payload = {"ok": 0, "msg": "离线可重试失败"}
                elif scenario == "search_error":
                    payload = {"ok": 0, "msg": "离线持续请求失败"}
                elif scenario == "empty" or number > 2:
                    payload = {"ok": 0, "msg": "这里还没有内容", "data": {"cards": []}}
                else:
                    notes = [mblog("known", long=True), mblog("short"), mblog("long", long=True)]
                    if number == 2:
                        invalid = mblog("text-only")
                        invalid["pics"] = []
                        notes = [mblog("short"), invalid]
                    payload = {"ok": 1, "data": {"cards": [{"card_type": 9, "mblog": item} for item in notes]}}
            elif parsed.path.startswith("/detail/"):
                if scenario in {"detail_403", "detail_429"}:
                    status = int(scenario.rsplit("_", 1)[1])
                elif scenario.startswith("repair_") or scenario == "detail_error":
                    content = b"no render data"
                elif scenario == "detail_retry" and counts[parsed.path] < 3:
                    status = 503
                else:
                    detail = mblog(parsed.path.rsplit("/", 1)[1])
                    content = ("var $render_data = " + json.dumps([{"status": detail}]) + "[0]").encode()
            else:
                assert parsed.hostname in {"i1.wp.com", "wx1.sinaimg.cn"}
                if scenario == "image_failure":
                    status = 404
                elif scenario == "image_rate_limit":
                    status = 429
                elif scenario == "image_retry" and sum(value for key, value in counts.items() if key.endswith(".jpg")) < 3:
                    status = 503
                elif scenario == "image_bad_bytes":
                    content = b"not an image"
                else:
                    content = image_bytes
            if payload is not None:
                content = json.dumps(payload).encode()
            return httpx.Response(status, content=content or b"offline failure", request=httpx.Request(method, url))

    options = settings(
        SAVE_DATA_PATH=str(data_root), SAVE_LOGIN_STATE=False, ENABLE_CDP_MODE=False,
        ENABLE_GET_MEIDAS=True, ENABLE_WEIBO_FULL_TEXT=True, COOKIES="SUB=t05", LOGIN_TYPE="cookie",
        CRAWLER_TYPE="detail" if scenario == "detail" or scenario.startswith("repair_") else "search",
        WEIBO_SPECIFIED_ID_LIST=["long"],
    )
    if legacy is not None:
        from tools.async_file_writer import utils as writer_utils
        for key, value in vars(options).items():
            monkeypatch.setattr(legacy.core.config, key, value)
        monkeypatch.setattr(legacy.core.config, "ENABLE_GET_COMMENTS", False)
        monkeypatch.setattr(legacy.core.config, "ENABLE_IP_PROXY", False)
        monkeypatch.setattr(legacy.client, "make_async_client", lambda **kwargs: HTTP())
        monkeypatch.setattr(legacy.client.WeiboClient.request.retry, "sleep", sleep)
        monkeypatch.setattr(legacy.client.WeiboClient.get_note_info_by_id.retry, "sleep", sleep)
        monkeypatch.setattr(legacy.core, "async_playwright", Browser)
        monkeypatch.setattr(legacy.core, "run_required_human_behavior", behavior)
        monkeypatch.setattr(legacy.utils, "get_current_timestamp", lambda: 1700000000000)
        monkeypatch.setattr(writer_utils, "get_current_date", lambda: "2026-09-30")
        legacy.bridge.install_weibo_browser_detail_fallback()
        crawler = legacy.core.WeiboCrawler()
    else:
        config, ports = entry.weibo_dependencies(options, post_repair=bool(post_repair))
        ports = replace(
            ports, client=replace(ports.client, make_async_client=lambda **kwargs: HTTP()),
            run_required_human_behavior=behavior, current_timestamp=lambda: 1700000000000,
            store_factory=lambda: JsonlContentStore(AsyncFileWriter(
                "weibo", config.CRAWLER_TYPE, save_data_path=lambda: str(data_root),
                current_date=lambda: "2026-09-30",
            )),
        )
        monkeypatch.setattr(core, "async_playwright", Browser)
        monkeypatch.setattr(client.WeiboClient.request.retry, "sleep", sleep)
        monkeypatch.setattr(client.WeiboClient._get_note_info_direct.retry, "sleep", sleep)
        crawler = core.WeiboCrawler(config, ports)
    failure = []
    try:
        await crawler.start()
    except Exception as exc:
        failure = exception_chain(exc)
    await crawler.close()
    artifacts = {str(path.relative_to(data_root)): path.read_bytes() for path in data_root.rglob("*") if path.is_file()}
    execution_events = json.loads(state.read_text())["events"]
    # 保存可审阅请求和事件；stealth 资源用哈希标识，图片与 JSONL 原字节保留在 batch 下。
    (directory / "trace.json").write_text(json.dumps(
        {"trace": trace, "events": execution_events, "failure": failure,
         "artifact_sha256": {name: sha256(value).hexdigest() for name, value in artifacts.items()}},
        ensure_ascii=False, indent=2,
        default=lambda value: {"sha256": sha256(value).hexdigest()} if isinstance(value, bytes) else str(value),
    ))
    return {"trace": trace, "artifacts": artifacts, "events": execution_events, "failure": failure}


@pytest.mark.asyncio
@pytest.mark.parametrize("post_repair", [0, 1])
@pytest.mark.parametrize("scenario", [
    "search", "detail", "empty", "search_retry", "search_error", "detail_retry", "detail_error",
    "detail_403", "detail_429", "image_failure", "image_rate_limit", "image_retry", "image_bad_bytes",
    "login_expired", "repair_api", "repair_page", "repair_empty", "repair_navigation", "repair_rate_limit",
])
async def test_frozen_implementation_matches_requests_artifacts_and_events(tmp_path, scenario, post_repair):
    with pytest.MonkeyPatch.context() as patch:
        legacy = load_baseline(tmp_path / "baseline", patch)
        old = await drive(legacy, tmp_path / "old", patch, scenario, post_repair)
    with pytest.MonkeyPatch.context() as patch:
        new = await drive(None, tmp_path / "new", patch, scenario, post_repair)
    assert new == old
    if scenario in {"search", "detail", "empty", "search_retry", "detail_retry", "login_expired"}:
        assert not new["failure"], new["failure"]
    requests = [row for row in new["trace"] if row[0] == "request"]
    assert all("avatar.invalid" not in row[2] for row in requests)
    assert not any("/detail/known" in row[2] for row in requests)
    for row in requests:
        assert row[3]["headers"]["Cookie"] == "SUB=t05"
        assert row[3]["headers"]["User-Agent"]
        assert row[3]["headers"]["Referer"]
    serialized = b"".join(new["artifacts"].values())
    assert b"avatar.invalid" not in serialized
    assert "截断".encode() not in serialized
    batches = [row for row in new["events"] if row["type"] == "adaptive_batch_completed"]
    assert all(row["details"]["stagnation_basis"] == "candidate_identity" for row in batches)
    if scenario == "search":
        records = [json.loads(line) for name, value in new["artifacts"].items() if "/jsonl/" in name for line in value.splitlines()]
        long = next(row for row in records if row["note_id"] == "long")
        assert long["content_detail_source"] == "mobile_detail"
        assert long["content"] == "青岛完整正文"
        assert long["followers_count"] == 0 and long["followers_observed"] is True
        stopped = new["events"][-1]
        assert stopped["type"] == "adaptive_search_stopped"
        assert stopped["details"]["stop_reason"] == "source_exhausted"
    if scenario.startswith("repair_"):
        detail_requests = [row for row in requests if "/detail/long" in row[2]]
        assert len(detail_requests) == 3
        browser_api = [row for row in new["trace"] if row[0] == "browser_api"]
        assert bool(browser_api) == bool(post_repair)
        if post_repair:
            assert new["trace"].index(browser_api[0]) > new["trace"].index(detail_requests[-1])
            assert browser_api[0][2] == "https://m.weibo.cn/statuses/show?id=long"
            if scenario == "repair_empty":
                # get_note_info_task 原本捕获 DataFetchError；另一个测试检查异常链及 attempts。
                assert not new["artifacts"]


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
                    assert module == "trippostcollect.runtime.helpers"
                if module.startswith("trippostcollect.platforms."):
                    assert module.startswith("trippostcollect.platforms.weibo")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["empty", "api_error", "navigation", "rate_limit", "no_page"])
async def test_repair_attempts_and_exception_chain_match_frozen_hook(tmp_path, scenario):
    async def execute(legacy, patch):
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
            def __init__(self):
                self.request = SimpleNamespace(get=self.api)

            async def api(self, url, **kwargs):
                trace.append(("api", url, kwargs))
                if scenario == "api_error":
                    raise ValueError("离线 API 解析失败")

                async def payload():
                    return {"id": "wrong", "text": "其他帖子"}

                return SimpleNamespace(status=429 if scenario == "rate_limit" else 200, json=payload)

            async def goto(self, url, **kwargs):
                trace.append(("goto", url, kwargs))
                if scenario == "navigation":
                    raise PlaywrightError("离线导航失败")

            async def wait_for_timeout(self, timeout):
                trace.append(("wait", timeout))

            async def evaluate(self, script, note_id):
                trace.append(("evaluate", script, note_id))
                return {"id": "wrong", "text": "其他帖子"}

        page = None if scenario == "no_page" else Page()
        patch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "1")
        patch.setenv("TRIPPOSTCOLLECT_WEIBO_BROWSER_DETAIL_TIMEOUT_MS", "100")
        if legacy:
            patch.setattr(legacy.client, "make_async_client", lambda **kwargs: HTTP())
            patch.setattr(legacy.client.WeiboClient.get_note_info_by_id.retry, "sleep", sleep)
            legacy.bridge.install_weibo_browser_detail_fallback()
            instance = legacy.client.WeiboClient(headers={"Cookie": "SUB=t05"}, playwright_page=page, cookie_dict={})
        else:
            ports = entry.weibo_dependencies(settings())[1].client
            ports = replace(ports, make_async_client=lambda **kwargs: HTTP())
            patch.setattr(client.WeiboClient._get_note_info_direct.retry, "sleep", sleep)
            instance = client.WeiboClient(headers={"Cookie": "SUB=t05"}, playwright_page=page, cookie_dict={}, ports=ports, post_repair=True)
        with pytest.raises(Exception) as error:
            await instance.get_note_info_by_id("123")
        return trace, exception_chain(error.value)

    with pytest.MonkeyPatch.context() as patch:
        old = await execute(load_baseline(tmp_path / "baseline", patch), patch)
    with pytest.MonkeyPatch.context() as patch:
        new = await execute(None, patch)
    assert new == old
    trace, errors = new
    assert len([row for row in trace if row[0] == "GET"]) == 3
    if scenario in {"empty", "api_error"}:
        api_attempt = "browser_api:http_200:empty_detail" if scenario == "empty" else "browser_api:ValueError"
        assert f"attempts=['{api_attempt}', 'page_state:empty_detail']" in errors[0][1]
        assert errors[1][0] == "DataFetchError"
    if scenario != "no_page":
        assert next(row for row in trace if row[0] == "api")[2]["timeout"] == 5000


@pytest.mark.parametrize("post_repair", ["0", "1"])
def test_worker_assembly_uses_root_weibo_without_fork_store(tmp_path, post_repair):
    from test_adapter_t02 import _json_tail, _old_argv, _run_python

    result = _json_tail(_run_python(
        "import os, sys, json\n"
        f"os.environ['TRIPPOSTCOLLECT_POST_REPAIR'] = {post_repair!r}\n"
        "from trippostcollect.platforms.entry import configure, install_hooks, load_crawler\n"
        f"configure({_old_argv('weibo_search', tmp_path)!r})\n"
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
