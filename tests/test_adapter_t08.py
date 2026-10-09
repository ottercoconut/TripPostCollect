"""T04-b 与迁移后的 B站正式链路使用同一套离线驱动逐项对照。"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from support import fork_removal_deviation
from trippostcollect.artifacts import image_materialization, image_proxy
from trippostcollect.platforms.bilibili import client, core, login, parser, signer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
executor = importlib.import_module("mediacrawler_crawl")


@pytest.fixture
def baseline(tmp_path):
    # 由 git show c283b1738002a57fd80075c9146f1b6064c021b1:scripts/mediacrawler_crawl.py
    # 逐字保存；纯源码 lane 没有 .git，仍必须导入完整旧实现而非重写参考算法。
    source = (ROOT / "tests/fixtures/adapter_t08/mediacrawler_crawl.py.txt").read_bytes()
    assert sha256(source).hexdigest() == "df19e2b8e508332399b762b58cf205754e67ff4fa1b133ccb06cec29f6b1fd4f"
    # T14-C 有意偏离：MEDIACRAWLER_DIR 与过渡模块 execution_state 已删除，执行前单向替换旧导入。
    text, namespace = fork_removal_deviation.frozen_source(source.decode("utf-8"))
    path = tmp_path / "baseline.py"
    path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("t08_baseline", path)
    module = importlib.util.module_from_spec(spec)
    vars(module).update(namespace)
    spec.loader.exec_module(module)
    return module


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 30, 1, 2, 3, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class Response(io.BytesIO):
    def __init__(self, content, url, media_type="application/json"):
        super().__init__(content)
        self.url = url
        self.headers = {"content-type": media_type, "content-length": str(len(content))}

    def geturl(self):
        return self.url

    def getcode(self):
        return 200


def drive(module, root, monkeypatch, scenario):
    """仅替换浏览器驱动、网络边界与时钟；保留解析、重试、暂存和摘要实现。"""
    root.mkdir()
    trace = []
    counters = Counter()
    db_path = root / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
        conn.execute("INSERT INTO web_posts VALUES ('bilibili', '99', NULL)")
    batch = root / "batch"
    state_path = root / "state.json"
    module.FrozenExecutionState.create(
        state_path, run_id="t08", job_key="bilibili-qingdao", site_key="bilibili",
        job_kind="mediacrawler_search", plan={}, frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    output = io.BytesIO()
    Image.new("RGB", (6, 5), color=(10, 20, 30)).save(output, format="PNG")
    image_bytes = output.getvalue()

    def item(post_id):
        return {
            "id": post_id, "title": "青岛旅游", "desc": "搜索摘要",
            "image_urls": ["https://preview.test/never.png"], "pubdate": 1_700_000_000,
            "like": 1, "reply": 2, "view": 3, "author": "作者", "mid": "456",
            "face": "https://avatar.test/never.png",
        }

    def open_request(request, timeout):
        headers = {name.lower(): value for name, value in request.header_items()}
        trace.append(("request", request.get_method(), request.full_url,
                      headers, timeout))
        assert "close" in [event[0] for event in trace]
        assert headers["cookie"] == "SESSDATA=t08;bili_jct=csrf"
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        key = url.path
        counters[key] += 1
        if key == "/x/web-interface/nav":
            payload = {"data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
            }}}
        elif key == "/x/web-interface/wbi/search/type":
            assert query["wts"] == ["1700000123"]
            assert len(query["w_rid"][0]) == 32
            assert query["page_size"] == ["20"]
            payload = {"code": 0, "data": {"result": [item("99"), item("123"), item("124")] if query["page"] == ["1"] else []}}
        elif key == "/x/article/view":
            assert query["id"] != ["99"]
            count = counters[key]
            if scenario.startswith("detail_http_"):
                raise HTTPError(request.full_url, int(scenario.rsplit("_", 1)[1]), "受阻", {}, None)
            if scenario.startswith("detail_block_") or (scenario.startswith("detail_retry_") and count < 3):
                payload = {"code": int(scenario.rsplit("_", 1)[1]), "message": "重试或阻断"}
            elif scenario == "detail_parse" and count <= 3:
                return Response(b"not-json", request.full_url)
            else:
                payload = {"code": 0, "data": {
                    "title": "青岛详情", "content": '<p>青岛第一段完整正文。</p><img src="https://i0.hdslb.com/bfs/article/html.png"><p>第二段。</p>',
                    "pubdate": 1_800_000_000,
                    "opus": {"content": {"paragraphs": [{"pic": {"pics": [
                        {"url": "http://i0.hdslb.com/bfs/article/opus.png"},
                    ]}}]}},
                    "content_pic_list": ["https://i0.hdslb.com/bfs/article/list.png", "https://i0.hdslb.com/bfs/article/opus.png"],
                    "origin_image_urls": ["https://cover.test/never.png"],
                }}
                if scenario == "detail_fallback":
                    payload["data"].update(
                        content="青岛完整正文", opus={}, content_pic_list=[],
                        origin_image_urls=["https://i0.hdslb.com/bfs/article/fallback.png"],
                    )
        elif key == "/x/relation/stat":
            assert query == {"vmid": ["456"]}
            if scenario.startswith("follower_block_"):
                payload = {"code": int(scenario.rsplit("_", 1)[1]), "message": "受阻"}
            else:
                payload = {"code": 0, "data": {"follower": 42}}
        elif key.startswith("/bfs/article/"):
            assert headers["referer"] in {"https://www.bilibili.com/read/cv123/", "https://www.bilibili.com/read/cv124/"}
            if scenario.startswith("image_http_") or (scenario == "image_retry" and counters[key] < 3):
                code = 503 if scenario == "image_retry" else int(scenario.rsplit("_", 1)[1])
                raise HTTPError(request.full_url, code, "图片响应", {}, None)
            return Response(b"invalid-image" if scenario == "image_invalid" else image_bytes, request.full_url, "image/png")
        else:
            raise AssertionError(request.full_url)
        return Response(json.dumps(payload, ensure_ascii=False).encode(), request.full_url)

    class Page:
        async def goto(self, url, **kwargs):
            trace.append(("goto", url, kwargs))

    class Context:
        pages = [Page()]

        async def add_init_script(self, **kwargs):
            trace.append(("init_script", sorted(kwargs)))

        async def route(self, pattern, handler):
            trace.append(("route", pattern))
            for resource_type, url in (("media", "https://example.test/v"), ("image", "https://example.test/a.mp4"), ("image", "https://example.test/a.png")):
                async def abort():
                    trace.append(("abort", url))

                async def proceed():
                    trace.append(("continue", url))

                await handler(SimpleNamespace(request=SimpleNamespace(resource_type=resource_type, url=url), abort=abort, continue_=proceed))

        async def cookies(self, urls):
            trace.append(("cookies", urls))
            return [{"name": "SESSDATA", "value": "t08"}, {"name": "bili_jct", "value": "csrf"}]

        async def close(self):
            trace.append(("close",))

    class Playwright:
        chromium = None

        async def __aenter__(self):
            self.chromium = self
            trace.append(("playwright_enter",))
            return self

        async def __aexit__(self, *args):
            trace.append(("playwright_exit",))

        async def launch_persistent_context(self, **kwargs):
            trace.append(("launch", kwargs))
            return Context()

    async def runtime_hints(context):
        trace.append(("runtime_hints",))

    async def behavior(page, **kwargs):
        trace.append(("behavior", kwargs["platform_key"], kwargs["profile_name"]))
        if scenario == "behavior_failed":
            raise RuntimeError("行为阶段失败")
        evidence = {"status": "completed", "finished_at": "2026-09-30T01:02:03+00:00",
                    "events": [{"event": name} for name in ("pause", "mouse_moves", "human_scroll_complete")]}
        kwargs["write_evidence"](kwargs["evidence_path"], evidence)
        return evidence

    for target in (module, core):
        monkeypatch.setattr(target, "datetime", FixedDatetime)
    monkeypatch.setattr(module.time, "time", lambda: 1700000123)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(module.time, "sleep", lambda delay: trace.append(("sleep", delay)))
    monkeypatch.setattr(module.random, "uniform", lambda low, high: (low + high) / 2)
    monkeypatch.setattr(module, "async_playwright", Playwright)
    monkeypatch.setattr(module, "discover_cdp_browser_path", lambda: "/fake/browser")
    monkeypatch.setattr(module, "profile_dir_for", lambda platform: root / "profile")
    monkeypatch.setattr(module, "cookie_snapshot_path", lambda platform: root / "snapshot.json")
    monkeypatch.setattr(module, "browser_launch_environment", lambda: {"LANG": "zh_CN.UTF-8"})
    monkeypatch.setattr(module, "browser_runtime_args", lambda: ["--test-runtime"])
    monkeypatch.setattr(module, "install_runtime_hints", runtime_hints)
    monkeypatch.setattr(module, "run_page_behavior", behavior)
    monkeypatch.setattr(module if module is not executor else client, "urlopen", open_request)
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(image_materialization.secrets, "token_hex", lambda size: "a" * (size * 2))
    original_load = module.load_existing_formal_identities

    def load_known(path):
        trace.append(("load_known",))
        return original_load(path)

    monkeypatch.setattr(module, "load_existing_formal_identities", load_known)
    original_validate = module.validate_formal_record

    def validate(platform, record, seen):
        trace.append(("validate", record["content_id"]))
        return original_validate(platform, record, seen)

    monkeypatch.setattr(module, "validate_formal_record", validate)
    sanitization_owner = module if module is not executor else core
    original_sanitize = sanitization_owner.sanitize_author_avatar_data

    def sanitize(record):
        trace.append(("sanitize", record["content_id"]))
        return original_sanitize(record)

    monkeypatch.setattr(sanitization_owner, "sanitize_author_avatar_data", sanitize)
    args = SimpleNamespace(keyword="青岛旅游 !'()*", db=str(db_path), headed=False,
                           start_page=1, top_refresh_max_pages=0, discovery_source_exhausted=False,
                           discovery_job_id=None, discovery_query_fingerprint="",
                           resume_identities_path=None, download_images=True)
    result = module.run_bilibili_article_search(args, batch)
    records = [json.loads(line) for path in batch.rglob("search_contents_*.jsonl") for line in path.read_text().splitlines()]
    files = {path.relative_to(batch).as_posix(): path.read_bytes() for path in batch.rglob("*") if path.is_file()}
    events = [(event["type"], event["details"]) for event in json.loads(state_path.read_text())["events"]]

    def normalize(value):
        if isinstance(value, bytes):
            return value.replace(str(root).encode(), b"<run>")
        if isinstance(value, str):
            return value.replace(str(root), "<run>")
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    return normalize({"trace": trace, "records": records, "files": files,
                      "events": events, "result": result})


SCENARIOS = ["success", "detail_fallback", "behavior_failed", "detail_parse", "image_retry", "image_invalid"] + [
    f"{kind}_{code}"
    for kind, codes in (
        ("detail_retry", (-509, -412, -352)),
        ("detail_block", (-101, -509, -412, -352)),
        ("detail_http", (401, 403, 429)),
        ("follower_block", (-101, -509, -412, -352)),
        ("image_http", (503, 404, 403, 429)),
    )
    for code in codes
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_formal_request_and_artifact_equivalence(baseline, tmp_path, monkeypatch, scenario):
    with monkeypatch.context() as patch:
        old = drive(baseline, tmp_path / "old", patch, scenario)
    with monkeypatch.context() as patch:
        new = drive(executor, tmp_path / "new", patch, scenario)
    assert new["files"].keys() == old["files"].keys()
    for path in new["files"]:
        assert new["files"][path] == old["files"][path], path
    assert new == old
    names = [event[0] for event in new["trace"]]
    assert names.index("load_known") < names.index("launch")
    if scenario == "behavior_failed":
        assert names.index("behavior") < names.index("close") < names.index("playwright_exit")
        assert "request" not in names
        assert new["result"]["ok"] is False
        return
    assert names.index("behavior") < names.index("cookies") < names.index("close") < names.index("request")
    requests = [event for event in new["trace"] if event[0] == "request"]
    assert all(event[1] == "GET" for event in requests)
    assert not any("never.png" in event[2] for event in requests)
    if scenario == "detail_fallback":
        assert new["result"]["ok"] is True
        assert all(record["detail_image_sources"] == ["detail_origin_image_urls"] for record in new["records"])
        assert len([path for path in new["files"] if "/images/" in path]) == 2
    if scenario in {"success", "image_retry"} or scenario.startswith("detail_retry"):
        assert new["result"]["ok"] is True, new["result"]["run"]["stderr_tail"]
        assert len(new["records"]) == 2
        assert new["records"][0]["published_at"] == 1_700_000_000
        assert new["records"][0]["detail_image_sources"] == ["opus_paragraph_pic", "content_html_img", "detail_content_pic_list"]
        assert len([path for path in new["files"] if "/images/" in path]) == 6
        assert sum("/x/relation/stat?" in event[2] for event in requests) == 1
        assert names.index("sanitize") < names.index("validate")
        request_paths = [urlparse(event[2]).path for event in requests]
        assert request_paths.index("/x/article/view") < request_paths.index("/bfs/article/opus.png") < request_paths.index("/x/relation/stat")
    if "block" in scenario or scenario.startswith("detail_http") or scenario in {"image_http_403", "image_http_429"}:
        assert new["result"]["run"]["returncode"] == 1
        stopped = [details for event, details in new["events"] if event == "adaptive_search_stopped"][-1]
        assert stopped["stop_reason"] == "runtime_failed"
        assert stopped["resume_page"] == 1
        assert "123" not in stopped["candidate_identities"]
    if scenario in {"image_http_503", "image_http_404", "image_invalid"}:
        failures = new["result"]["image_materialization"]["skipped_candidate_failures"]
        assert len(failures) == 2
        assert all(failure["retryable"] == (scenario == "image_http_503") for failure in failures)
        assert all(failure["attempts"] == (3 if scenario == "image_http_503" else 1) for failure in failures)


def test_dependency_direction_and_legacy_exports():
    for path in (ROOT / "src/trippostcollect/platforms/bilibili").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                assert not module.startswith(("scripts", "mediacrawler_crawl", "trippostcollect.runtime", "trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler")), (path, module)
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
    for implementation in (client, parser, signer):
        for name, value in vars(implementation).items():
            if callable(value) and getattr(value, "__module__", None) == implementation.__name__ and name != "fetch_bilibili_image_bytes":
                assert getattr(executor, name) is value
    assert executor._bilibili_login is login


def test_function_bodies_only_add_ports(baseline):
    old = ast.parse(Path(baseline.__file__).read_text())
    original = {node.name: node for node in old.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for implementation in (core, client, parser, signer, login):
        for node in ast.parse(Path(implementation.__file__).read_text()).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            while body and isinstance(body[0], ast.Assign) and isinstance(body[0].value, ast.Attribute) and isinstance(body[0].value.value, ast.Name) and body[0].value.value.id == "ports":
                body = body[1:]
            reference = original[node.name].body
            if node.name == "run_bilibili_article_search":
                # 唯一调用表达式变化：相同读取时点和路径，经执行器注入 connect。
                for expression in ast.walk(ast.Module(body=reference, type_ignores=[])):
                    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute) and isinstance(expression.func.value, ast.Name) and expression.func.value.id == "sqlite3" and expression.func.attr == "connect":
                        expression.func = ast.Name(id="connect_database", ctx=ast.Load())
            assert ast.dump(ast.Module(body=body, type_ignores=[])) == ast.dump(ast.Module(body=reference, type_ignores=[])), node.name
