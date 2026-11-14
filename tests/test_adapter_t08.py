"""B站正式链路的离线驱动：行为会话、搜索与详情请求、阻断即停、正文图片与暂存产物。"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timezone
import importlib
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

from trippostcollect.artifacts import image_materialization, image_proxy
from trippostcollect.platforms.bilibili import client, core


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
executor = importlib.import_module("mediacrawler_crawl")


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


def drive(root, monkeypatch, scenario):
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
    executor.FrozenExecutionState.create(
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
        url = urlparse(request.full_url)
        if url.path.startswith("/bfs/"):
            # #115：图片请求不带平台 Cookie。
            assert "cookie" not in headers
        else:
            assert headers["cookie"] == "SESSDATA=t08;bili_jct=csrf"
        query = parse_qs(url.query)
        key = url.path
        counters[key] += 1
        if key == "/x/web-interface/nav":
            payload = {"code": 0, "data": {"isLogin": True, "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
            }}}
        elif key == "/x/web-interface/wbi/search/type":
            assert query["wts"] == ["1700000123"]
            assert len(query["w_rid"][0]) == 32
            assert query["page_size"] == ["20"]
            payload = {"code": 0, "data": {
                "result": [item("99"), item("123"), item("124")] if query["page"] == ["1"] else [],
                "numPages": 1, "numResults": 3,
            }}
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

        async def evaluate(self, script):
            # #127：关闭 context 前读取浏览器 UA；求值不进入轨迹。
            assert script == "() => navigator.userAgent", script
            return BROWSER_USER_AGENT

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

    for target in (executor, core):
        monkeypatch.setattr(target, "datetime", FixedDatetime)
    monkeypatch.setattr(executor.time, "time", lambda: 1700000123)
    monkeypatch.setattr(executor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(executor.time, "sleep", lambda delay: trace.append(("sleep", delay)))
    monkeypatch.setattr(executor.random, "uniform", lambda low, high: (low + high) / 2)
    monkeypatch.setattr(executor, "async_playwright", Playwright)
    monkeypatch.setattr(executor, "discover_cdp_browser_path", lambda: "/fake/browser")
    monkeypatch.setattr(executor, "profile_dir_for", lambda platform: root / "profile")
    monkeypatch.setattr(executor, "cookie_snapshot_path", lambda platform: root / "snapshot.json")
    monkeypatch.setattr(executor, "browser_launch_environment", lambda: {"LANG": "zh_CN.UTF-8"})
    monkeypatch.setattr(executor, "browser_runtime_args", lambda: ["--test-runtime"])
    monkeypatch.setattr(executor, "install_runtime_hints", runtime_hints)
    monkeypatch.setattr(executor, "run_page_behavior", behavior)
    monkeypatch.setattr(client, "urlopen", open_request)
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(image_materialization.secrets, "token_hex", lambda size: "a" * (size * 2))
    original_load = executor.load_existing_formal_identities

    def load_known(path):
        trace.append(("load_known",))
        return original_load(path)

    monkeypatch.setattr(executor, "load_existing_formal_identities", load_known)
    original_validate = executor.validate_formal_record

    def validate(platform, record, seen):
        trace.append(("validate", record["content_id"]))
        return original_validate(platform, record, seen)

    monkeypatch.setattr(executor, "validate_formal_record", validate)
    original_sanitize = core.sanitize_author_avatar_data

    def sanitize(record):
        trace.append(("sanitize", record["content_id"]))
        return original_sanitize(record)

    monkeypatch.setattr(core, "sanitize_author_avatar_data", sanitize)
    args = SimpleNamespace(keyword="青岛旅游 !'()*", db=str(db_path), headed=False,
                           start_page=1, top_refresh_max_pages=0, discovery_source_exhausted=False,
                           discovery_job_id=None, discovery_query_fingerprint="",
                           resume_identities_path=None, download_images=True)
    result = executor.run_bilibili_article_search(args, batch)
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


BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
EXHAUSTED = {("source_exhausted", "empty_page")}
BOTH = ["123", "124"]
# #115：-509/-412/-352 与 HTTP 429 命中即停，不重试。
STOP_ON_FIRST = frozenset(
    [f"detail_retry_{code}" for code in (-509, -412, -352)]
    + [f"detail_block_{code}" for code in (-101, -509, -412, -352)]
    + [f"detail_http_{code}" for code in (401, 403, 429)]
    + [f"follower_block_{code}" for code in (-101, -509, -412, -352)]
)


def expected_outcome(scenario):
    """(成功, 停止事件集合, 跳过的候选 [(范围, 错误码)], 入库 ID, 落盘正文图数；None 表示不核对)。"""
    if scenario in STOP_ON_FIRST:
        kind, code = scenario.rsplit("_", 1)
        stage = "relation_stat" if kind == "follower_block" else "article_detail"
        # 粉丝请求在第一篇正文图下载之后。
        return False, {("runtime_failed", f"bilibili_{stage}_blocked:{code}")}, [], [], 3 if stage == "relation_stat" else 0
    return {
        "success": (True, EXHAUSTED, [], BOTH, 6),
        "image_retry": (True, EXHAUSTED, [], BOTH, 6),
        # 封面不作正文图：记录入库但没有正文图。
        "detail_fallback": (True, EXHAUSTED, [], BOTH, 0),
        "behavior_failed": (False, {("runtime_failed", "RuntimeError")}, [], [], 0),
        "detail_parse": (True, EXHAUSTED, [("post", "detail_request_failed")], ["124"], 3),
        "image_invalid": (True, EXHAUSTED, [("image", "image_non_raster_response")] * 2, [], None),
        "image_http_503": (True, EXHAUSTED, [("image", "image_download_retryable")] * 2, [], 0),
        "image_http_404": (True, EXHAUSTED, [("image", "image_source_unavailable")] * 2, [], 0),
        "image_http_403": (False, {("runtime_failed", "image_auth_required")}, [], [], 0),
        "image_http_429": (False, {("runtime_failed", "image_rate_limited")}, [], [], 0),
    }[scenario]


def drive_outcome(new, scenario):
    stops = {(details["stop_reason"], details.get("stop_detail"))
             for event, details in new["events"] if event == "adaptive_search_stopped"}
    skips = [(details.get("failure_scope"), details.get("error_code"))
             for event, details in new["events"] if event == "candidate_skipped"]
    images = sum("/images/" in path for path in new["files"])
    return (new["result"]["ok"], stops, skips, [record["content_id"] for record in new["records"]],
            None if expected_outcome(scenario)[4] is None else images)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_formal_request_and_artifacts(tmp_path, monkeypatch, scenario):
    with monkeypatch.context() as patch:
        new = drive(tmp_path / "new", patch, scenario)
    assert drive_outcome(new, scenario) == expected_outcome(scenario)
    assert new["result"]["run"]["returncode"] == (0 if new["result"]["ok"] else 1)
    assert ("logs/bilibili/behavior_evidence.json" in new["files"]) is (scenario != "behavior_failed")
    stopped = [details for event, details in new["events"] if event == "adaptive_search_stopped"]
    if stopped and stopped[-1]["stop_reason"] == "source_exhausted":
        assert (stopped[-1]["source_num_pages"], stopped[-1]["source_num_results"]) == (1, 3)
    requests = [event for event in new["trace"] if event[0] == "request"]
    assert not any("cookie" in event[3] for event in requests if urlparse(event[2]).path.startswith("/bfs/"))
    assert all(event[3]["user-agent"] == BROWSER_USER_AGENT for event in requests)
    if scenario == "detail_fallback":
        assert all(record["detail_image_sources"] == [] for record in new["records"])
        assert not any(urlparse(event[2]).path.startswith("/bfs/") for event in requests)
        return
    if scenario in STOP_ON_FIRST:
        path = "/x/relation/stat" if scenario.startswith("follower_block") else "/x/article/view"
        assert sum(urlparse(event[2]).path == path for event in requests) == 1
        assert stopped[-1]["resume_page"] == 1
        assert stopped[-1]["candidate_identities"] == []
        return
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
    if scenario in {"success", "image_retry"}:
        assert new["result"]["ok"] is True, new["result"]["run"]["stderr_tail"]
        assert len(new["records"]) == 2
        assert new["records"][0]["published_at"] == 1_700_000_000
        assert new["records"][0]["detail_image_sources"] == ["opus_paragraph_pic", "content_html_img", "detail_content_pic_list"]
        assert len([path for path in new["files"] if "/images/" in path]) == 6
        assert sum("/x/relation/stat?" in event[2] for event in requests) == 1
        assert names.index("sanitize") < names.index("validate")
        request_paths = [urlparse(event[2]).path for event in requests]
        assert request_paths.index("/x/article/view") < request_paths.index("/bfs/article/opus.png") < request_paths.index("/x/relation/stat")
    if scenario in {"image_http_403", "image_http_429"}:
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


def test_dependency_direction():
    for path in (ROOT / "src/trippostcollect/platforms/bilibili").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            for module in modules:
                assert not module.startswith(("scripts", "mediacrawler_crawl", "trippostcollect.runtime", "trippostcollect.artifacts", "trippostcollect.db", "trippostcollect.scheduler")), (path, module)
                if module.startswith("trippostcollect.application"):
                    assert module == "trippostcollect.application.contracts"
