"""知乎 T07：根适配在离线边界下运行，与 T14 固化的冻结旧实现请求、事件与字节逐项比对。"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import fields, replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import execjs
import httpx
import pytest

from support import legacy_expectations as expectations
from support.stable_png import solid_png
from trippostcollect.application import events
from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.artifacts import jsonl
from trippostcollect.core import resources
from trippostcollect.platforms import entry
from trippostcollect.platforms.zhihu import client, core, signer
from trippostcollect.runtime import image_retry


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/adapter_t07"
SIGNATURES = {}


async def drive(output, patch, scenario, *, crawler_factory=None):
    """只替换 HTTP、Playwright、时钟和 JS 随机源，保留业务及全部重试层；配置取根配置对象，不加载 fork。"""
    output.mkdir()
    trace = []
    counts = Counter()
    execution = output / "state.json"
    execution.write_text('{"events": []}')
    patch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(execution))
    patch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    for name in ("TRIPPOSTCOLLECT_DB_PATH", "TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH",
                 "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID", "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT"):
        patch.delenv(name, raising=False)
    patch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    patch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    patch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "0")
    patch.setattr(events, "_utc_iso", lambda: "2026-09-30T01:02:03+00:00")
    patch.setattr(jsonl.time, "strftime", lambda *args: "2026-09-30")
    patch.setattr(image_retry, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0))
    # 根配置对象与 fork config 在装配读取的键上逐键相同（T12 守护）；词云等退出键根对象没有。
    config = worker_config()
    for name, value in dict(
        PLATFORM="zhihu", KEYWORDS="青岛旅游", LOGIN_TYPE="cookie", COOKIES="d_c0=t07;z_c0=active",
        CRAWLER_TYPE="detail" if scenario.startswith("detail_") else "search",
        ENABLE_CDP_MODE=True, ENABLE_IP_PROXY=False, ENABLE_GET_MEIDAS=True,
        ENABLE_GET_COMMENTS=False, ENABLE_GET_SUB_COMMENTS=False, ENABLE_GET_WORDCLOUD=False,
        START_PAGE=1, MAX_CONCURRENCY_NUM=1, CRAWLER_MAX_SLEEP_SEC=0,
        SAVE_DATA_OPTION="jsonl", SAVE_DATA_PATH=str(output / "data"),
        ZHIHU_SPECIFIED_ID_LIST=["https://www.zhihu.com/question/10/answer/101", "https://zhuanlan.zhihu.com/p/102"],
    ).items():
        # 只有词云这一个 fork 退出键在根配置对象上不存在，仅对它放宽；其余键缺失即报错。
        patch.setattr(config, name, value, raising=name != "ENABLE_GET_WORDCLOUD")

    async def sleep(seconds):
        trace.append(("sleep", float(seconds)))

    patch.setattr(core.asyncio, "sleep", sleep)
    # tenacity 保留 stop/wait/reraise；只截获等待，不改变重试次数。
    patch.setattr(client.ZhiHuClient.request.retry, "sleep", sleep)

    class Page:
        url = "about:blank"

        def is_closed(self):
            return False

        async def goto(self, url, **kwargs):
            self.url = url
            trace.append(("goto", url, kwargs))
            if scenario == "navigation_timeout":
                raise TimeoutError("导航超时")

        async def reload(self, **kwargs):
            trace.append(("reload", kwargs))

        async def wait_for_load_state(self, state, **kwargs):
            trace.append(("ready", state, kwargs))

    page = Page()

    class Context:
        pages = [page]

        async def new_page(self):
            trace.append(("new_page",))
            return page

        async def add_cookies(self, cookies):
            trace.append(("add_cookies", cookies))

        async def cookies(self, urls=None):
            trace.append(("cookies", urls))
            return [{"name": "d_c0", "value": "t07"}, {"name": "z_c0", "value": "active"}]

    context = Context()

    class Browser:
        async def launch_and_connect(self, **kwargs):
            trace.append(("launch", {k: v for k, v in kwargs.items() if k != "playwright"}))
            return context

        async def get_browser_info(self):
            return {"driver": "CDP"}

        async def cleanup(self):
            trace.append(("close",))

    class Playwright:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            trace.append(("playwright_exit",))

    async def behavior(observed_page, platform):
        trace.append(("behavior", observed_page.url, platform))

    compile_js = execjs.compile

    def compile_fixed(source):
        trace.append(("compile", sha256(source.encode()).hexdigest()))
        js = compile_js("Math.random = () => 0.25;\n" + source)

        def call(name, uri, cookie):
            trace.append(("sign", uri, cookie))
            # 相同资源和输入的固定随机签名缓存，减少 Node 启动；两侧都核验资源散列。
            key = (sha256(source.encode()).hexdigest(), name, uri, cookie)
            if key not in SIGNATURES:
                SIGNATURES[key] = js.call(name, uri, cookie)
            return dict(SIGNATURES[key])

        return SimpleNamespace(call=call)

    patch.setattr(execjs, "compile", compile_fixed)
    patch.setattr(signer, "ZHIHU_SGIN_JS", None)
    # 与主机无关的纯色 PNG（见 support/stable_png.py；Pillow 压缩字节随 CPU 不同）。
    image_bytes = solid_png(5, 4, (10, 20, 30))

    def entity(identity, kind="answer", *, full=False):
        return {
            "id": identity, "type": kind, "question": {"id": "10"},
            "title": "青岛旅游", "excerpt": "青岛搜索摘要", "created_time": 1_700_000_000,
            "updated_time": 1_700_000_001,
            "content": ("<p>青岛正文第一段</p><figure><img src=\"https://pic1.zhimg.com/v2-" + identity +
                        "_r.jpg\"><img src=\"https://pic2.zhimg.com/v2-" + identity +
                        "_720w.webp\"><figcaption>不应进入正文</figcaption></figure><p>青岛正文第二段</p>") if full else "",
            "author": {"id": "author", "name": "测试作者", "url_token": "author", "follower_count": 42,
                       "avatar_url": "https://avatar.test/never.jpg"},
        }

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            trace.append(("request", method, url, kwargs))
            parsed = urlsplit(url)
            key = parsed.path
            counts[key] += 1
            status = 200
            payload = None
            text = None
            if key == "/api/v4/me":
                status = 401 if scenario == "login_expired" and counts[key] <= 3 else 200
                payload = {"uid": "author", "name": "作者"}
            elif key == "/api/v4/search_v3":
                offset = int(parse_qs(parsed.query)["offset"][0])
                if scenario == "search_retry" and counts[key] <= 2:
                    status = 503
                if scenario == "search_block":
                    status = 429
                payload = {"data": [] if scenario == "empty" or offset >= 40 else [
                    {"type": "search_result", "object": entity("101" if offset == 0 else "102", "answer" if offset == 0 else "article",
                                                              full=scenario == "search_content")},
                ]}
            else:
                identity = key.rsplit("/", 1)[-1]
                if scenario in {"detail_404", "detail_429", "detail_401", "search_detail_429", "search_detail_401"}:
                    status = int(scenario.rsplit("_", 1)[-1])
                if scenario in {"detail_retry", "detail_failed"} and (scenario == "detail_failed" or counts[key] <= 2):
                    status = 503
                kind = "article" if key.startswith("/p/") else "answer"
                target = entity(identity, kind, full=scenario != "detail_parse")
                payload = {"initialState": {"entities": {kind + "s": {"wrong": entity("wrong", kind, full=True), identity: target}}}}
                text = '<script id="js-initialData">' + json.dumps(payload) + "</script>"
            request = httpx.Request(method, url)
            return httpx.Response(status, request=request, text=text) if text else httpx.Response(status, request=request, json=payload)

        def stream(self, method, url, **kwargs):
            trace.append(("image", method, url, kwargs))
            counts[url] += 1
            status = 200
            if scenario == "image_failed" and "101" in url:
                status = 503
            if scenario == "image_block":
                status = 403
            if scenario == "image_retry" and counts[url] < 3:
                status = 503

            class Stream:
                status_code = status

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                def raise_for_status(self):
                    httpx.Response(status, request=httpx.Request(method, url)).raise_for_status()

                async def aiter_bytes(self):
                    yield image_bytes

            return Stream()

    def http_factory(**kwargs):
        trace.append(("http_client", kwargs))
        return Http()

    settings, ports = entry._zhihu_dependencies(config)
    client_ports = ports.client_factory.keywords["ports"]
    ports = replace(
        ports, async_playwright=Playwright, browser_manager_factory=Browser,
        run_required_human_behavior=behavior, current_timestamp=lambda: 1_700_000_000_000,
        client_factory=lambda **kwargs: client.ZhiHuClient(
            **kwargs, ports=replace(client_ports, make_async_client=http_factory),
        ),
    )
    if crawler_factory is None:
        crawler = core.ZhihuCrawler(settings, ports)
    else:
        patch.setattr(entry, "_zhihu_dependencies", lambda config: (settings, ports))
        crawler = crawler_factory()
    assert not trace
    await crawler.start()
    await crawler.close()
    files = {str(path.relative_to(output / "data")): path.read_bytes()
             for path in sorted((output / "data").rglob("*")) if path.is_file()}
    records = [json.loads(line) for path, content in files.items() if "/jsonl/" in path for line in content.decode().splitlines()]
    return {"trace": trace, "files": files, "records": records,
            "events": json.loads(execution.read_text())["events"], "counts": counts}


SCENARIOS = ["success", "navigation_timeout", "search_content", "empty", "search_retry", "search_block",
             "search_detail_429", "search_detail_401",
             "detail_success", "detail_retry", "detail_failed", "detail_parse", "detail_404", "detail_401", "detail_429",
             "image_failed", "image_retry", "image_block", "login_expired"]


T14_DRIVE = ("T07", "request_event_artifacts")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_root_matches_frozen_legacy_request_event_and_artifacts(tmp_path, monkeypatch, scenario):
    """T14：根实现与固化的旧实现结果比较（同一 drive、同一 == 语义），不加载 fork。"""
    with monkeypatch.context() as patch:
        new = await drive(tmp_path / "new", patch, scenario)
    assert expectations.scrub(new, (tmp_path, "<TMP>")) == expectations.load(*T14_DRIVE, scenario)
    check_drive_result(new, scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_root_entry_matches_frozen_legacy_factory(tmp_path, monkeypatch, scenario):
    """T14：替代旧工厂对照；新入口装配结果与固化预期比较。"""
    with monkeypatch.context() as patch:
        new = await drive(tmp_path / "entry", patch, scenario,
                          crawler_factory=lambda: entry.load_crawler("zhihu")())
    assert expectations.scrub(new, (tmp_path, "<TMP>")) == expectations.load(*T14_FACTORY, scenario)


def check_drive_result(new, scenario):
    trace = new["trace"]
    names = [event[0] for event in trace]
    assert names.count("compile") == 1
    behavior_index = names.index("behavior")
    assert trace[behavior_index][1] == "https://www.zhihu.com/search?q=" + quote("青岛旅游") + "&type=content"
    assert names[behavior_index - 1] == "cookies"
    assert "launch" in names
    requests = [event for event in trace if event[0] == "request"]
    assert all(event[1] == "GET" for event in requests)
    assert all(event[3]["headers"]["x-zse-96"] and event[3]["headers"]["x-zst-81"] for event in requests)
    assert all(event[3]["headers"]["x-zse-96"].startswith("2.0_") and event[3]["headers"]["x-zst-81"].startswith("3_2.0") for event in requests)
    assert all(event[3]["headers"]["cookie"] == "d_c0=t07;z_c0=active" for event in requests)
    assert b"avatar.test" not in b"".join(new["files"].values())
    if scenario in {"success", "navigation_timeout", "search_content", "search_retry", "image_retry", "login_expired"}:
        assert len(new["records"]) == 2
        assert all(record["image_count"] == 1 and len(record["image_assets"]) == 1 for record in new["records"])
        assert all(record["content_text"] == "青岛正文第一段\n青岛正文第二段" for record in new["records"])
        assert new["events"][-1]["details"]["stop_reason"] == "source_exhausted"
        assert new["counts"]["/api/v4/search_v3"] == (5 if scenario == "search_retry" else 3)
    if scenario == "detail_404":
        assert new["counts"]["/question/10/answer/101"] == 1
        assert new["records"] == []
    if scenario in {"detail_retry", "detail_failed", "detail_401", "detail_429"}:
        assert new["counts"]["/question/10/answer/101"] == 3
    if scenario in {"search_block", "image_block", "search_detail_429", "search_detail_401"}:
        assert new["events"][-1]["details"]["stop_reason"] == "runtime_failed"
    if scenario == "image_failed":
        assert [record["content_id"] for record in new["records"]] == ["102"]
        assert any(event["type"] == "candidate_skipped" for event in new["events"])
    if scenario == "login_expired":
        assert new["counts"]["/api/v4/me"] == 3
        assert names.count("add_cookies") == 4
    if scenario == "empty":
        assert new["records"] == []
        assert new["events"][-1]["details"]["stop_reason"] == "source_exhausted"
    if scenario == "detail_success":
        assert [record["content_id"] for record in new["records"]] == ["101", "102"]
        assert [record["content_detail_source"] for record in new["records"]] == ["answer_detail", "article_detail"]
        assert new["events"] == []


def test_dependency_direction():
    allowed_runtime = {"trippostcollect.runtime.cookies", "trippostcollect.runtime.helpers", "trippostcollect.runtime.image_retry"}
    allowed_names = {"convert_cookies", "convert_str_cookie_to_dict", "extract_text_from_html", "normalize_image_url",
                     "ImageDownloadFetchError", "IMAGE_DOWNLOAD_MAX_BYTES", "classified_http_image_error", "is_runtime_blocking_image_error"}
    for path in (ROOT / "src/trippostcollect/platforms/zhihu").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                assert module.split(".")[0] not in {"scripts", "config", "tools", "media_platform", "store", "base", "var", "model", "constant", "proxy"}, (path, module)
                assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler", "patchright")), (path, module)
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
                if module.startswith("trippostcollect.platforms"):
                    assert module.startswith("trippostcollect.platforms.zhihu")
                if module.startswith("trippostcollect.runtime"):
                    assert module in allowed_runtime
                    assert {alias.name for alias in node.names} <= allowed_names


def test_worker_assembly_without_fork_zhihu(tmp_path):
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text())
    # 从已有父侧 argv golden 复用私有参数，启动点只到构造，不 start。
    argv = commands["zhihu_search"]["cmd"][4:]
    code = (
        "import sys,json\n"
        "from trippostcollect.platforms.entry import configure,install_hooks,load_crawler\n"
        f"configure({argv!r})\ninstall_hooks()\n"
        "cls=load_crawler('zhihu'); crawler=cls()\n"
        "assert cls.__module__ == 'trippostcollect.platforms.zhihu.core'\n"
        "assert not any(n == 'media_platform.zhihu' or n.startswith('media_platform.zhihu.') or n == 'store.zhihu' or n.startswith('store.zhihu.') for n in sys.modules)\n"
        "assert crawler.settings.ENABLE_CDP_MODE\n"
        "print(json.dumps([load_crawler(c).__name__ for c in ['wb','dy','xhs']]))\n"
    )
    result = subprocess.run([sys.executable, "-P", "-c", code], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == ["WeiboCrawler", "DouYinCrawler", "XiaoHongShuCrawler"]


def test_signer_is_lazy_and_uses_identical_resource(monkeypatch):
    calls = []
    monkeypatch.setattr(signer, "ZHIHU_SGIN_JS", None)
    monkeypatch.setattr(execjs, "compile", lambda source: calls.append(source) or SimpleNamespace(call=lambda *args: args))
    assert calls == []
    assert signer.sign("/first", "d_c0=test") == ("get_sign", "/first", "d_c0=test")
    assert signer.sign("/second", "d_c0=next") == ("get_sign", "/second", "d_c0=next")
    assert calls == [resources.read_text("js/zhihu.js")]
    assert resources.read_bytes("js/zhihu.js") == (FIXTURE / "libs/zhihu.js.txt").read_bytes()


def test_settings_are_snapshotted_and_operation_readers_are_lazy(monkeypatch):
    # T14：根配置对象取代 fork config（逐键相同由 T12 守护）；本用例只验证根装配快照与读取时机。
    config = worker_config()
    original = {field.name: getattr(config, field.name) for field in fields(entry._zhihu_dependencies(config)[0])}
    settings, ports = entry._zhihu_dependencies(config)
    monkeypatch.setattr(config, "KEYWORDS", "青岛崂山")
    assert settings.KEYWORDS == original["KEYWORDS"]
    monkeypatch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "2.5")
    assert ports.initial_settle_seconds() == 2.5
    monkeypatch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "bad")
    assert ports.initial_settle_seconds() == 0


ISSUE_49_IDENTITY_REPLACEMENTS = (
    ("anonymize_user_id(", "platform_user_id("),
    ("mask_nickname(", "platform_nickname("),
    ('"Creator anonymized hash"', '"Creator raw platform user ID"'),
    ('"User nickname (masked)"', '"User raw nickname"'),
)


def test_pure_definitions_keep_original_bodies():
    """模型、签名、解析定义只允许来源模块与 logger 名称替换。"""
    ledger = json.loads((ROOT / "docs/adapter-ledger/symbols.json").read_text())["rows"]

    def definitions(source):
        result = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result[node.name] = node
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            result[f"{node.name}.{child.name}"] = child
        return result

    for row in ledger:
        if row["card"] != "T07" or row["target"] not in {
            "platforms/zhihu/parser.py", "platforms/zhihu/models.py", "platforms/zhihu/signer.py",
        }:
            continue
        name = row["qualname"]
        if name in {"ZhihuExtractor", "update_zhihu_content"}:
            continue
        original = (FIXTURE / (row["file"].removeprefix("tools/MediaCrawler/") + ".txt")).read_text()
        original = original.replace("utils.logger", "logger")
        # #49 有意偏离：作者 ID/昵称改存平台原始值，只允许身份转换调用与对应字段描述替换。
        for old, new in ISSUE_49_IDENTITY_REPLACEMENTS:
            original = original.replace(old, new)
        current = (ROOT / "src/trippostcollect" / row["target"]).read_text()
        assert ast.dump(definitions(original)[name]) == ast.dump(definitions(current)[name]), name


# 旧 fork 工厂结果与冻结 T07 fixture 结果相同已在 T14 删除 fork 前由守卫分别证明，共用一份预期。
T14_FACTORY = T14_DRIVE
