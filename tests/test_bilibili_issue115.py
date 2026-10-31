"""#115 B站 article 修复的防回归测试：delta 正文、封面不作正文图、来源耗尽证据、登录态、
阻断即停、搜索/nav 错误分类、粉丝失败不入缓存、翻页节奏、图片不带 Cookie 与重定向白名单。

只替换浏览器会话与 urllib 网络边界；夹具 `tests/fixtures/bilibili/article_delta_bodies.json` 是从库内
只读取出的 3 条 delta 正文，只保留 ops（不含头像、作者、Cookie）。
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

from trippostcollect.application.contracts import BilibiliImageFetchPorts
from trippostcollect.artifacts import image_proxy
from trippostcollect.platforms.bilibili import client as bilibili_client
from trippostcollect.platforms.bilibili import core as bilibili_core
from trippostcollect.platforms.bilibili import parser as bilibili_parser


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

mediacrawler_crawl = import_module("mediacrawler_crawl")
DELTA_FIXTURES = json.loads(
    (ROOT / "tests/fixtures/bilibili/article_delta_bodies.json").read_text(encoding="utf-8")
)


class Response(io.BytesIO):
    def __init__(self, payload: dict, url: str):
        super().__init__(json.dumps(payload, ensure_ascii=False).encode())
        self.url = url
        self.headers = {"content-type": "application/json"}

    def geturl(self) -> str:
        return self.url


def search_item(post_id: str, mid: str = "456") -> dict:
    return {
        "id": post_id, "title": "青岛旅游", "desc": "搜索摘要",
        "pubdate": 1_700_000_000, "like": 1, "reply": 2, "view": 3,
        "author": "作者", "mid": mid,
    }


def detail_payload(content: str | None = None) -> dict:
    return {"code": 0, "data": {
        "title": "青岛详情",
        "content": content or "<p>青岛完整正文第一段。</p>",
        "content_pic_list": ["https://i0.hdslb.com/bfs/article/body.png"],
        "opus": {"content": {"paragraphs": []}},
    }}


class Network:
    """按路径路由的 urllib 替身；search 是逐次返回的搜索 data 列表（或异常）。"""

    def __init__(self, *, search=None, nav=None, follower=None, detail=None):
        self.search = list(search or [])
        self.nav = nav if nav is not None else {"code": 0, "data": {"isLogin": True, "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }}}
        self.follower = follower or {}
        self.detail = detail or {}
        self.calls: Counter[str] = Counter()

    def __call__(self, request, timeout):
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        if url.path == "/x/web-interface/nav":
            self.calls["nav"] += 1
            if isinstance(self.nav, Exception):
                raise self.nav
            return Response(self.nav, request.full_url)
        if url.path == "/x/web-interface/wbi/search/type":
            self.calls["search"] += 1
            value = self.search.pop(0)
            if isinstance(value, Exception):
                raise value
            return Response(value, request.full_url)
        if url.path == "/x/article/view":
            self.calls["detail"] += 1
            value = self.detail.get(query["id"][0])
            if isinstance(value, Exception):
                raise value
            return Response(value or detail_payload(), request.full_url)
        if url.path == "/x/relation/stat":
            creator = query["vmid"][0]
            self.calls[f"follower:{creator}"] += 1
            value = self.follower.get(creator)
            if isinstance(value, list):
                value = value.pop(0)
            if isinstance(value, Exception):
                raise value
            return Response(value or {"code": 0, "data": {"follower": 42}}, request.full_url)
        raise AssertionError(f"未登记的请求：{request.full_url}")


def search_data(items: list[dict], *, num_pages: int, num_results: int, **extra) -> dict:
    return {"code": 0, "data": {"result": items, "numPages": num_pages, "numResults": num_results, **extra}}


def prepare(monkeypatch, tmp_path: Path, network: Network, *, keep_wbi: bool = False):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)")
    state_path = tmp_path / "state.json"
    mediacrawler_crawl.FrozenExecutionState.create(
        state_path, run_id="issue115", job_key="bili-115", site_key="bilibili",
        job_kind="mediacrawler_search", plan={}, frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        mediacrawler_crawl, "run_bilibili_behavior_session",
        AsyncMock(return_value=({"cookie_header": "SESSDATA=test", "user_agent": "Mozilla/5.0 test-browser"}, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    if not keep_wbi:
        monkeypatch.setattr(bilibili_core, "fetch_bilibili_wbi_keys", lambda value, **kwargs: ("a" * 32, "b" * 32))
    monkeypatch.setattr(bilibili_client, "urlopen", network)
    sleeps: list[float] = []
    monkeypatch.setattr(bilibili_core.time, "sleep", sleeps.append)
    monkeypatch.setattr(bilibili_client.time, "sleep", sleeps.append)
    args = SimpleNamespace(
        keyword="青岛旅游", db=str(db_path), headed=False, start_page=1, top_refresh_max_pages=0,
        discovery_source_exhausted=False, discovery_job_id=None,
        discovery_query_fingerprint="", resume_identities_path=None,
    )
    return args, state_path, sleeps


def events_of(state_path: Path, kind: str) -> list[dict]:
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


# ---------- D1：新编辑器 delta 正文 ----------

@pytest.mark.parametrize("fixture", DELTA_FIXTURES, ids=lambda item: item["platform_post_id"])
def test_delta_body_is_joined_text_not_raw_json(fixture):
    text = bilibili_parser.bilibili_delta_to_text(fixture["content"])
    ops = json.loads(fixture["content"])["ops"]
    expected = "".join(op["insert"] for op in ops if isinstance(op["insert"], str))

    assert text
    assert '"ops"' not in text and '"insert"' not in text
    # 只做空白规范化：每段文字按原顺序保留，换行没有被压成一行。
    assert [line.strip() for line in text.split("\n") if line.strip()] == [
        line.strip() for line in expected.replace("\xa0", " ").split("\n") if line.strip()
    ]
    assert bilibili_parser.clean_bilibili_article_body(fixture["content"]) == text


def test_delta_text_keeps_paragraphs_and_drops_embeds():
    raw = json.dumps({"ops": [
        {"insert": {"native-image": {"url": "https://i0.hdslb.com/bfs/article/a.jpg"}}},
        {"insert": "第一段\n\n"},
        {"insert": "加粗", "attributes": {"bold": True}},
        {"insert": "同段\n"},
        {"insert": {"cut-off": {"type": "0"}}},
        {"insert": "第二段\n"},
    ]}, ensure_ascii=False)

    assert bilibili_parser.bilibili_delta_to_text(raw) == "第一段\n\n加粗同段\n第二段"


@pytest.mark.parametrize("raw", [
    "<p>旧编辑器 HTML</p>",
    "普通文本",
    '{"not_ops": []}',
    '{"ops": "x"}',
    "{broken json",
    "",
])
def test_delta_to_text_returns_none_for_non_delta(raw):
    assert bilibili_parser.bilibili_delta_to_text(raw) is None


def test_delta_without_any_text_is_no_body():
    raw = json.dumps({"ops": [{"insert": {"native-image": {"url": "https://i0.hdslb.com/bfs/article/a.jpg"}}},
                              {"insert": "\n \n"}]})

    assert bilibili_parser.bilibili_delta_to_text(raw) is None
    assert bilibili_parser.clean_bilibili_article_body(raw) == ""


def test_delta_detail_without_text_is_detail_parse_failure(monkeypatch):
    raw = json.dumps({"ops": [{"insert": {"native-image": {"url": "https://i0.hdslb.com/bfs/article/a.jpg"}}}]})
    monkeypatch.setattr(
        bilibili_client, "urlopen",
        lambda request, timeout: Response(detail_payload(raw), request.full_url),
    )

    with pytest.raises(bilibili_client.BilibiliArticleDetailError) as caught:
        bilibili_client.fetch_bilibili_article_detail("1", user_agent="Mozilla/5.0 test-browser")

    # 沿用现有“无可解析正文”失败码：可重试，耗尽后形成 candidate_skipped。
    assert caught.value.retryable is True
    assert caught.value.code == 0
    assert "no parseable body" in str(caught.value)


def test_delta_images_dedupe_with_opus_by_bfs_asset():
    # 库内 delta 文章里，同一张图在 opus 段落（biliimg、无后缀）和 delta 嵌入（hdslb、@webp 变换）各出现一次。
    content = json.dumps({"ops": [
        {"insert": {"native-image": {"url": "https://i0.hdslb.com/bfs/article/abc123.jpg@progressive.webp"}}},
        {"insert": "正文\n"},
        {"insert": {"native-image": {"url": "http://i0.hdslb.com/bfs/new_dyn/only-in-delta.jpg"}}},
    ]})
    detail = {
        "content": content,
        "opus": {"content": {"paragraphs": [
            {"pic": {"pics": [{"url": "https://article.biliimg.com/bfs/article/abc123.jpg"}]}},
        ]}},
        "origin_image_urls": ["https://i0.hdslb.com/bfs/article/banner/cover.png"],
    }

    images, sources = bilibili_parser.extract_bilibili_detail_images(detail)

    assert images == [
        "https://article.biliimg.com/bfs/article/abc123.jpg",
        "https://i0.hdslb.com/bfs/new_dyn/only-in-delta.jpg",
    ]
    assert sources == ["opus_paragraph_pic", "content_delta_image"]


@pytest.mark.parametrize("fixture", DELTA_FIXTURES, ids=lambda item: item["platform_post_id"])
def test_delta_fixture_images_come_from_native_image_embeds(fixture):
    ops = json.loads(fixture["content"])["ops"]
    embedded = [op["insert"]["native-image"]["url"] for op in ops
                if isinstance(op["insert"], dict) and "native-image" in op["insert"]]

    images, sources = bilibili_parser.extract_bilibili_detail_images({"content": fixture["content"]})

    assert len(images) == len({bilibili_parser.bilibili_bfs_asset_key(url) for url in embedded})
    assert set(sources) <= {"content_delta_image"}


def test_delta_record_hydrates_text_and_body_images():
    fixture = next(item for item in DELTA_FIXTURES if "native-image" in item["content"])
    search = mediacrawler_crawl.normalize_bilibili_article_record(search_item(fixture["platform_post_id"]), "青岛")
    record = mediacrawler_crawl.hydrate_bilibili_article_record(search, {"content": fixture["content"]}, attempts=1)

    assert not record["content_text"].startswith("{")
    assert record["content_length"] == len(record["content_text"])
    assert record["image_urls"] and set(record["detail_image_sources"]) == {"content_delta_image"}


def test_delta_to_text_is_exported_for_backfill():
    assert mediacrawler_crawl.bilibili_delta_to_text is bilibili_parser.bilibili_delta_to_text


# ---------- D2：封面不作正文图 ----------

def test_cover_is_never_a_body_image_fallback():
    detail = {
        "content": "<p>只有文字</p>",
        "origin_image_urls": ["https://i0.hdslb.com/bfs/article/banner/cover.png"],
        "image_urls": ["https://i0.hdslb.com/bfs/article/banner/cover.png"],
        "opus": {"content": {"paragraphs": []}},
    }

    assert bilibili_parser.extract_bilibili_detail_images(detail) == ([], [])


def test_text_only_article_is_invalid_record_not_run_failure(monkeypatch, tmp_path):
    # 0 张正文图的文章由正式门禁判为 missing_content_image（候选无效），不拖垮整轮或误判耗尽。
    network = Network(
        search=[search_data([search_item("101")], num_pages=1, num_results=1),
                search_data([], num_pages=1, num_results=1)],
        detail={"101": {"code": 0, "data": {
            "content": "<p>青岛纯文字</p>",
            "origin_image_urls": ["https://i0.hdslb.com/bfs/article/banner/cover.png"],
        }}},
    )
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    record = json.loads(next((tmp_path / "batch").rglob("*.jsonl")).read_text(encoding="utf-8"))
    assert record["image_urls"] == []
    validation = mediacrawler_crawl.validate_formal_record("bilibili", record, set())
    assert validation["valid"] is False and validation["reasons"] == ["missing_content_image"]
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_reason"] == "source_exhausted"


# ---------- S7：来源耗尽必须有平台证据 ----------

def test_empty_result_past_num_pages_is_source_exhausted(monkeypatch, tmp_path):
    network = Network(search=[search_data([search_item("101")], num_pages=1, num_results=1),
                              search_data([], num_pages=1, num_results=1)])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert (stopped["stop_reason"], stopped["stop_detail"]) == ("source_exhausted", "empty_page")
    assert (stopped["source_page"], stopped["source_num_pages"], stopped["source_num_results"]) == (2, 1, 1)


def test_first_page_with_zero_results_is_source_exhausted(monkeypatch, tmp_path):
    network = Network(search=[search_data([], num_pages=0, num_results=0)])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert (stopped["stop_reason"], stopped["stop_detail"]) == ("source_exhausted", "no_results")
    assert (stopped["source_num_pages"], stopped["source_num_results"]) == (0, 0)


@pytest.mark.parametrize("data, detail", [
    ({"result": [], "numPages": 5, "numResults": 100}, "bilibili_article_search_empty_within_num_pages"),
    ({"result": []}, "bilibili_article_search_empty_without_num_pages"),
])
def test_empty_result_without_exhaustion_evidence_is_runtime_failure(monkeypatch, tmp_path, data, detail):
    network = Network(search=[{"code": 0, "data": data}])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("runtime_failed", detail)]
    assert stopped[0]["resume_page"] == 1


def test_v_voucher_response_is_verification_block_not_exhaustion(monkeypatch, tmp_path):
    network = Network(search=[search_data([], num_pages=0, num_results=0, v_voucher="voucher-x")])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls["search"] == 1
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("captcha_detected", "bilibili_article_search_risk_control:v_voucher")
    ]


# ---------- S8：登录态以 nav isLogin 为准 ----------

@pytest.mark.parametrize("data", [{"isLogin": False}, {}, None])
def test_nav_without_is_login_is_login_required(monkeypatch, data):
    payload = {"code": -101, "data": data} if data is None else {"code": 0, "data": {**data, "wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/a.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/b.png"}}}
    monkeypatch.setattr(bilibili_client, "urlopen", lambda request, timeout: Response(payload, request.full_url))

    with pytest.raises(bilibili_client.BilibiliRuntimeBlocked) as caught:
        bilibili_client.fetch_bilibili_wbi_keys("SESSDATA=stale", user_agent="Mozilla/5.0 test-browser")

    assert caught.value.stop_reason == "login_required"
    assert caught.value.detail == "bilibili_nav_login_required"


def test_stale_session_stops_run_as_login_required_before_search(monkeypatch, tmp_path):
    network = Network(nav={"code": -101, "message": "账号未登录", "data": {"isLogin": False}})
    args, state_path, _ = prepare(monkeypatch, tmp_path, network, keep_wbi=True)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls == Counter({"nav": 1})
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("login_required", "bilibili_nav_login_required")
    ]


def test_logged_in_nav_returns_wbi_keys(monkeypatch):
    network = Network()
    monkeypatch.setattr(bilibili_client, "urlopen", network)

    assert bilibili_client.fetch_bilibili_wbi_keys("SESSDATA=ok", user_agent="Mozilla/5.0 test-browser") == ("a" * 32, "b" * 32)
    assert network.calls == Counter({"nav": 1})


# ---------- 阻断码命中即停；搜索 / nav 错误分类 ----------

@pytest.mark.parametrize("code", [-509, -412, -352, -101])
def test_detail_blocking_code_is_not_retried(monkeypatch, code):
    network = Network(detail={"1": {"code": code, "message": "受阻"}})
    monkeypatch.setattr(bilibili_client, "urlopen", network)
    monkeypatch.setattr(bilibili_client.time, "sleep", lambda value: pytest.fail("阻断后不得退避重试"))

    with pytest.raises(bilibili_client.BilibiliArticleDetailError) as caught:
        bilibili_client.fetch_bilibili_article_detail_with_retry("1", user_agent="Mozilla/5.0 test-browser")

    assert network.calls["detail"] == 1
    assert caught.value.runtime_blocking is True and caught.value.attempts == 1


def test_detail_http_429_is_not_retried(monkeypatch):
    network = Network(detail={"1": HTTPError("https://api.bilibili.com/x/article/view", 429, "频繁", {}, None)})
    monkeypatch.setattr(bilibili_client, "urlopen", network)
    monkeypatch.setattr(bilibili_client.time, "sleep", lambda value: pytest.fail("阻断后不得退避重试"))

    with pytest.raises(bilibili_client.BilibiliArticleDetailError) as caught:
        bilibili_client.fetch_bilibili_article_detail_with_retry("1", user_agent="Mozilla/5.0 test-browser")

    assert network.calls["detail"] == 1
    assert caught.value.runtime_blocking is True


def test_detail_server_error_is_still_retried(monkeypatch):
    responses = [HTTPError("u", 503, "忙", {}, None), HTTPError("u", 502, "忙", {}, None)]

    def open_request(request, timeout):
        if responses:
            raise responses.pop(0)
        return Response(detail_payload(), request.full_url)

    monkeypatch.setattr(bilibili_client, "urlopen", open_request)
    monkeypatch.setattr(bilibili_client.time, "sleep", lambda value: None)

    _, attempts, _ = bilibili_client.fetch_bilibili_article_detail_with_retry("1", user_agent="Mozilla/5.0 test-browser")

    assert attempts == 3


@pytest.mark.parametrize("fault, expected_calls, detail", [
    (HTTPError("u", 412, "风控", {}, None), 1, "bilibili_article_search_blocked:412"),
    ({"code": -352, "message": "风控校验失败"}, 1, "bilibili_article_search_blocked:-352"),
    (TimeoutError("timed out"), 3, "bilibili_article_search_failed:network_unreachable"),
])
def test_search_errors_are_classified(monkeypatch, tmp_path, fault, expected_calls, detail):
    network = Network(search=[fault] * 3)
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls["search"] == expected_calls
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("runtime_failed", detail)]


def test_search_transient_error_recovers_within_retry_budget(monkeypatch, tmp_path):
    network = Network(search=[URLError("断连"),
                              search_data([search_item("101")], num_pages=1, num_results=1),
                              search_data([], num_pages=1, num_results=1)])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    assert network.calls["search"] == 3
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_reason"] == "source_exhausted"


@pytest.mark.parametrize("fault, expected_calls, detail", [
    (HTTPError("u", 412, "风控", {}, None), 1, "bilibili_nav_blocked:412"),
    (URLError("断连"), 3, "bilibili_nav_failed:network_unreachable"),
])
def test_nav_errors_are_classified(monkeypatch, tmp_path, fault, expected_calls, detail):
    network = Network(nav=fault)
    args, state_path, _ = prepare(monkeypatch, tmp_path, network, keep_wbi=True)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls["nav"] == expected_calls
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("runtime_failed", detail)]


# ---------- 粉丝数失败不入缓存，并保留错误码 ----------

def test_follower_failure_is_not_cached_for_same_author(monkeypatch, tmp_path):
    network = Network(
        search=[search_data([search_item("101", "456"), search_item("102", "456")], num_pages=1, num_results=2),
                search_data([], num_pages=1, num_results=2)],
        # #129：网络传输失败重试用尽后已是运行级停止；这里用可重试且不阻断的 HTTP 503 验证不缓存。
        follower={"456": [HTTPError("u", 503, "忙", {}, None)] * 3 + [{"code": 0, "data": {"follower": 7}}]},
    )
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    # 第一篇 3 次失败后跳过；第二篇同作者重新请求一次并成功。
    assert network.calls["follower:456"] == 4
    skipped = events_of(state_path, "candidate_skipped")
    assert [(item["identity"], item["error_code"]) for item in skipped] == [("101", "503")]
    records = [json.loads(line) for line in next((tmp_path / "batch").rglob("*.jsonl")).read_text().splitlines()]
    assert [(record["content_id"], record["author_followers_count"]) for record in records] == [("102", 7)]


def test_follower_permanent_code_is_kept_in_skip_event(monkeypatch, tmp_path):
    network = Network(
        search=[search_data([search_item("101", "456")], num_pages=1, num_results=1),
                search_data([], num_pages=1, num_results=1)],
        follower={"456": {"code": -404, "message": "用户不存在"}},
    )
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert network.calls["follower:456"] == 1
    skipped = events_of(state_path, "candidate_skipped")
    assert [(item["error_code"], item["retryable"]) for item in skipped] == [("-404", False)]


def test_follower_unexpected_exception_is_not_swallowed(monkeypatch, tmp_path):
    network = Network(search=[search_data([search_item("101")], num_pages=1, num_results=1)])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)

    def broken(creator_id, cookie_header, **kwargs):
        raise KeyError("编程错误不能被当作粉丝缺失")

    monkeypatch.setattr(bilibili_core, "fetch_bilibili_follower_count", broken)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert not events_of(state_path, "candidate_skipped")
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_detail"] == "KeyError"


# ---------- 搜索翻页节奏 ----------

def test_search_pages_are_paced_like_detail_requests(monkeypatch, tmp_path):
    network = Network(search=[search_data([search_item("101")], num_pages=2, num_results=2),
                              search_data([search_item("102")], num_pages=2, num_results=2),
                              search_data([], num_pages=2, num_results=2)])
    args, state_path, sleeps = prepare(monkeypatch, tmp_path, network)
    monotonic = iter(float(value) for value in range(1000))
    monkeypatch.setattr(bilibili_core.time, "monotonic", lambda: next(monotonic) * 0.0)
    ranges: list[tuple[float, float]] = []

    def uniform(low, high):
        ranges.append((low, high))
        return high

    monkeypatch.setattr(bilibili_core.random, "uniform", uniform)

    result = mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is True
    # 第 2、3 页请求前各等一次，节奏取自详情请求的同一配置。
    page_waits = [item for item in result["behavior_evidence"]["request_pacing_events"]
                  if item["stage"] == "article_search"]
    assert [item["source_page"] for item in page_waits] == [2, 3]
    assert all(item["pacing_wait_seconds"] == bilibili_core.BILIBILI_DETAIL_PACING_SECONDS[1] for item in page_waits)
    assert ranges.count(bilibili_core.BILIBILI_DETAIL_PACING_SECONDS) >= 2
    assert sleeps.count(bilibili_core.BILIBILI_DETAIL_PACING_SECONDS[1]) >= 2


# ---------- P6：图片不带 Cookie，重定向只跟 B站图床 ----------

class ImageResponse(io.BytesIO):
    def __init__(self, url: str):
        super().__init__(b"\x89PNG\r\n\x1a\nfake")
        self.url = url
        self.headers = {"content-type": "image/png", "content-length": str(len(self.getvalue()))}

    def geturl(self):
        return self.url

    def getcode(self):
        return 200


def image_ports():
    return BilibiliImageFetchPorts(
        fetch_remote_image_bytes=image_proxy.fetch_remote_image_bytes,
        DEFAULT_ARCHIVE_IMAGE_MAX_BYTES=1024,
        SUPPORTED_IMAGE_MIME_TYPES=frozenset({"image/png"}),
    )


def redirecting_opener(chain: dict[str, str], seen: list[tuple[str, dict]]):
    def open_request(request, timeout):
        headers = {name.lower(): value for name, value in request.header_items()}
        seen.append((request.full_url, headers))
        if request.full_url in chain:
            raise HTTPError(request.full_url, 302, "Found", {"location": chain[request.full_url]}, None)
        return ImageResponse(request.full_url)

    return SimpleNamespace(open=open_request)


def test_formal_run_image_requests_carry_no_session_cookie(monkeypatch, tmp_path):
    # 整轮跑：详情/粉丝请求带会话 Cookie，图片请求经共享图片下载器发往 CDN 时不带。
    network = Network(search=[search_data([search_item("101")], num_pages=1, num_results=1),
                              search_data([], num_pages=1, num_results=1)])
    args, state_path, _ = prepare(monkeypatch, tmp_path, network)
    args.download_images = True
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: redirecting_opener({}, seen))
    monkeypatch.setattr(
        mediacrawler_crawl, "write_staging_image",
        lambda chunks, **kwargs: SimpleNamespace(
            path=kwargs["staging_root"] / (kwargs["relative_stem"] + ".png"), size_bytes=1,
            mime_type="image/png", width=1, height=1, sha256="0" * 64,
        ),
    )

    mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert [url for url, _ in seen] == ["https://i0.hdslb.com/bfs/article/body.png"]
    assert "cookie" not in seen[0][1]


def test_image_request_carries_no_platform_cookie(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: redirecting_opener({}, seen))

    preview = bilibili_client.fetch_bilibili_image_bytes(
        "https://i0.hdslb.com/bfs/article/a.png", "123", user_agent="Mozilla/5.0 test-browser", ports=image_ports(),
    )

    assert preview.final_url == "https://i0.hdslb.com/bfs/article/a.png"
    assert "cookie" not in seen[0][1]
    assert seen[0][1]["referer"] == "https://www.bilibili.com/read/cv123/"


def test_image_redirect_within_bilibili_cdn_is_followed(monkeypatch):
    seen: list[tuple[str, dict]] = []
    chain = {"https://i0.hdslb.com/bfs/article/a.png": "https://article.biliimg.com/bfs/article/a.png"}
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: redirecting_opener(chain, seen))

    preview = bilibili_client.fetch_bilibili_image_bytes(
        "https://i0.hdslb.com/bfs/article/a.png", "123", user_agent="Mozilla/5.0 test-browser", ports=image_ports(),
    )

    assert preview.final_url == "https://article.biliimg.com/bfs/article/a.png"
    assert all("cookie" not in headers for _, headers in seen)


@pytest.mark.parametrize("target", [
    "https://evil.example.test/a.png",
    "https://hdslb.com.evil.test/a.png",
    "https://nothdslb.com/a.png",
])
def test_image_redirect_off_bilibili_cdn_is_refused(monkeypatch, target):
    seen: list[tuple[str, dict]] = []
    chain = {"https://i0.hdslb.com/bfs/article/a.png": target}
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: redirecting_opener(chain, seen))

    with pytest.raises(image_proxy.RemoteImageFetchError) as caught:
        bilibili_client.fetch_bilibili_image_bytes(
            "https://i0.hdslb.com/bfs/article/a.png", "123", user_agent="Mozilla/5.0 test-browser", ports=image_ports(),
        )

    assert caught.value.code == "image_source_unavailable"
    assert caught.value.retryable is False
    assert [url for url, _ in seen] == ["https://i0.hdslb.com/bfs/article/a.png"]


def test_redirect_allowlist_is_opt_in_for_other_callers(monkeypatch):
    seen: list[tuple[str, dict]] = []
    chain = {"https://cdn.example.test/a.png": "https://other.example.test/a.png"}
    monkeypatch.setattr(image_proxy, "build_opener", lambda *args: redirecting_opener(chain, seen))

    preview = image_proxy.fetch_remote_image_bytes(
        "https://cdn.example.test/a.png", headers={}, max_bytes=1024, timeout_seconds=1,
    )

    assert preview.final_url == "https://other.example.test/a.png"


def test_uncalled_chrome_124_preview_path_is_removed():
    for name in ("fetch_remote_image_preview", "_remote_image_headers", "REMOTE_IMAGE_REFERERS",
                 "DEFAULT_REMOTE_IMAGE_MAX_BYTES"):
        assert not hasattr(image_proxy, name), name
