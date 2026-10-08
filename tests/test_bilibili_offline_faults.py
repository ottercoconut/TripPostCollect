"""B站正式 article 链路的离线故障特征化（T13）：只替换浏览器会话与 urllib 网络边界。

固定现行行为，不改变重试层次与分类：搜索页失败为运行级失败且不宣称耗尽；
详情与粉丝请求的传输层异常按原 3 次重试后转为候选 skip；头像不进入任何产物。
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from collections import Counter
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from trippostcollect.platforms.bilibili import client as bilibili_client
from trippostcollect.platforms.bilibili import core as bilibili_core


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")
AVATAR_URL = "https://avatar.t13.test/face-never.png"


class Response(io.BytesIO):
    def __init__(self, payload: dict, url: str):
        super().__init__(json.dumps(payload, ensure_ascii=False).encode())
        self.url = url
        self.headers = {"content-type": "application/json"}

    def geturl(self) -> str:
        return self.url


def search_item(post_id: str, mid: str) -> dict:
    return {
        "id": post_id,
        "title": "青岛旅游",
        "desc": "搜索摘要",
        "image_urls": ["https://preview.test/never.png"],
        "pubdate": 1_700_000_000,
        "like": 1,
        "reply": 2,
        "view": 3,
        "author": "作者",
        "mid": mid,
        # 已登记头像键与同记录内经其证明的同 URL 重复字段（AGENTS 头像边界）。
        "avatar": AVATAR_URL,
        "face": AVATAR_URL,
    }


def detail_payload() -> dict:
    return {"code": 0, "data": {
        "title": "青岛详情",
        "content": "<p>青岛完整正文第一段。</p><p>第二段。</p>",
        "pubdate": 1_700_000_000,
        "image_urls": ["https://i0.hdslb.com/bfs/article/body.png"],
        "author": {"mid": 456, "name": "作者", "face": AVATAR_URL},
        "opus": {"content": {"paragraphs": []}},
    }}


class Network:
    """按路径路由的 urllib 替身；未登记的 URL 直接失败。"""

    def __init__(self, items: dict[int, list[dict]], *, search=None, detail=None, follower=None):
        self.items = items
        self.search = search or {}
        self.detail = detail or {}
        self.follower = follower or {}
        self.calls: Counter[str] = Counter()

    def __call__(self, request, timeout):
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        if url.path == "/x/web-interface/wbi/search/type":
            self.calls["search"] += 1
            fault = self.search.get(int(query["page"][0]))
            if fault == "transport":
                raise URLError("离线网络中断")
            if fault == "http_412":
                raise HTTPError(request.full_url, 412, "风控", {}, None)
            if fault == "code":
                return Response({"code": -412, "message": "请求被拦截"}, request.full_url)
            page_items = self.items.get(int(query["page"][0]), [])
            return Response({"code": 0, "data": {"result": page_items}}, request.full_url)
        if url.path == "/x/article/view":
            post_id = query["id"][0]
            self.calls[f"detail:{post_id}"] += 1
            if self.detail.get(post_id) == "transport":
                raise URLError("离线网络中断")
            return Response(detail_payload(), request.full_url)
        if url.path == "/x/relation/stat":
            creator = query["vmid"][0]
            self.calls[f"follower:{creator}"] += 1
            if self.follower.get(creator) == "transport":
                raise URLError("离线网络中断")
            return Response({"code": 0, "data": {"follower": 42}}, request.full_url)
        raise AssertionError(f"未登记的请求：{request.full_url}")


def prepare(monkeypatch, tmp_path: Path, network: Network, *, real_behavior: bool = False):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path, run_id="t13-bili", job_key="bili-t13", site_key="bilibili",
        job_kind="mediacrawler_search", plan={}, frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    if not real_behavior:
        monkeypatch.setattr(
            mediacrawler_crawl, "run_bilibili_behavior_session",
            AsyncMock(return_value=({"cookie_header": ""}, {"ok": True})),
        )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(bilibili_core, "fetch_bilibili_wbi_keys", lambda value: ("a" * 32, "b" * 32))
    monkeypatch.setattr(bilibili_client, "urlopen", network)
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)
    args = SimpleNamespace(
        keyword="青岛旅游", db=str(db_path), headed=False, start_page=1, top_refresh_max_pages=0,
        discovery_source_exhausted=False, discovery_job_id=None,
        discovery_query_fingerprint="", resume_identities_path=None,
    )
    return args, state_path


def events_of(state_path: Path, kind: str) -> list[dict]:
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


@pytest.mark.parametrize("fault", ["code", "http_412", "transport"])
def test_search_page_failure_is_runtime_failure_without_exhaustion(monkeypatch, tmp_path, fault):
    network = Network({1: [search_item("101", "456")]}, search={1: fault})
    args, state_path = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls == Counter({"search": 1})
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [event["stop_reason"] for event in stopped] == ["runtime_failed"]
    expected = {"code": "RuntimeError", "http_412": "HTTPError", "transport": "URLError"}[fault]
    assert stopped[0]["stop_detail"] == expected
    assert stopped[0]["resume_page"] == 1
    assert stopped[0]["source_has_more"] is True
    assert stopped[0]["discovery_phase"] == "frontier"


def test_later_search_page_failure_keeps_resume_at_failed_page(monkeypatch, tmp_path):
    network = Network({1: [search_item("101", "456")]}, search={2: "transport"})
    args, state_path = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "runtime_failed"
    assert stopped["stop_detail"] == "URLError"
    assert stopped["resume_page"] == 2
    assert not [event for event in events_of(state_path, "adaptive_search_stopped")
                if event["stop_reason"] == "source_exhausted"]


def test_detail_transport_error_retries_then_skips_candidate(monkeypatch, tmp_path):
    network = Network(
        {1: [search_item("101", "456"), search_item("102", "789")]},
        detail={"101": "transport"},
    )
    args, state_path = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert network.calls["detail:101"] == bilibili_client.BILIBILI_DETAIL_MAX_ATTEMPTS == 3
    assert network.calls["detail:102"] == 1
    skipped = events_of(state_path, "candidate_skipped")
    assert [event["identity"] for event in skipped] == ["101"]
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["candidate_identities"] == ["101", "102"]
    assert stopped["skipped_candidate_count"] == 1
    assert result["status"] == "completed"


def test_follower_transport_error_is_swallowed_retried_then_skips_candidate(monkeypatch, tmp_path):
    # 现行 core 以 except Exception 吞掉粉丝请求的非分类异常：不升级为运行阻断，3 次后候选 skip。
    network = Network(
        {1: [search_item("101", "456"), search_item("102", "789")]},
        follower={"456": "transport"},
    )
    args, state_path = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert network.calls["follower:456"] == 3
    assert network.calls["follower:789"] == 1
    skipped = events_of(state_path, "candidate_skipped")
    assert [(event["identity"], event["detail"]) for event in skipped] == [("101", "creator_profile_failed")]
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "source_exhausted"
    assert result["status"] == "completed"


def test_browser_launch_failure_stops_run_before_any_platform_request(monkeypatch, tmp_path):
    launches = []

    class Playwright:
        chromium = None

        async def __aenter__(self):
            self.chromium = self
            return self

        async def __aexit__(self, *args):
            return False

        async def launch_persistent_context(self, **kwargs):
            launches.append(kwargs["user_data_dir"])
            raise RuntimeError("浏览器启动失败")

    network = Network({1: [search_item("101", "456")]})
    args, state_path = prepare(monkeypatch, tmp_path, network, real_behavior=True)
    monkeypatch.setattr(mediacrawler_crawl, "async_playwright", Playwright)
    monkeypatch.setattr(mediacrawler_crawl, "discover_cdp_browser_path", lambda: "/fake/browser")
    monkeypatch.setattr(mediacrawler_crawl, "profile_dir_for", lambda platform: tmp_path / "profile")
    monkeypatch.setattr(mediacrawler_crawl, "browser_runtime_args", lambda: [])
    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert launches == [str(tmp_path / "profile")]
    assert result["ok"] is False
    assert network.calls == Counter()
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(event["stop_reason"], event["stop_detail"]) for event in stopped] == [
        ("runtime_failed", "RuntimeError")
    ]


def test_author_avatar_never_reaches_bilibili_artifacts(monkeypatch, tmp_path, capsys):
    network = Network({1: [search_item("101", "456")]})
    args, state_path = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["status"] == "completed"
    records = [json.loads(line) for path in (tmp_path / "batch").rglob("*.jsonl")
               for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["content_id"] for record in records] == ["101"]
    assert records[0]["author_followers_count"] == 42
    assert records[0]["user_id"] == "456"
    marker = AVATAR_URL.encode()
    for path in (tmp_path / "batch").rglob("*"):
        if path.is_file():
            assert marker not in path.read_bytes(), path
    assert marker not in state_path.read_bytes()
    captured = capsys.readouterr()
    assert AVATAR_URL not in captured.out + captured.err
    assert AVATAR_URL not in json.dumps(result, ensure_ascii=False, default=str)
