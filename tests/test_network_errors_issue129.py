"""#129：通用四平台浏览器外请求重试用尽后仍是传输层失败，按运行级 ``network_unreachable`` 停止。

DNS、连接、超时、连接重置是网络整体故障，与候选无关：不写 ``candidate_skipped``，不进候选记忆
（seen），worker 不崩溃到摘要缺失；runner 把带该停止细节的子摘要分类为 ``network_timeout`` /
``retry_wait``。重试次数与间隔不变；HTTP 状态码、解码失败与正文图片路径不在本条范围内。
测试只替换 httpx / urllib 网络边界与搜索客户端，不访问网络、不启动浏览器。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.error import URLError

import httpx
import pytest

from trippostcollect.application.contracts import ZhihuClientPorts
from trippostcollect.application.failures import classify_attempt
from trippostcollect.platforms.bilibili import client as bilibili_client
from trippostcollect.platforms.douyin import core as douyin_core
from trippostcollect.platforms.douyin.client import DouYinClient
from trippostcollect.platforms.douyin.models import PlatformRuntimeError as DouyinRuntimeError
from trippostcollect.platforms.weibo.client import WeiboClient
from trippostcollect.platforms.zhihu.client import ZhiHuClient
from trippostcollect.platforms.zhihu.models import PlatformRuntimeError as ZhihuRuntimeError

from platforms.douyin import test_douyin_offline_faults as douyin_faults
from platforms.weibo import test_weibo_offline_faults as weibo_faults
from platforms.zhihu import test_zhihu_offline_faults as zhihu_faults
import test_bilibili_issue115 as bili115


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


def assert_runtime_network_stop(state_path, identity: str, expected_detail: str) -> dict:
    """停止事件是运行级 network_unreachable，且故障候选既不是 skip，也不在候选记忆里。"""

    stopped = events_of(state_path, "adaptive_search_stopped")
    assert stopped, "必须写出停止事件，摘要才有失败原因"
    last = stopped[-1]
    assert last["stop_reason"] == "runtime_failed"
    assert last["stop_detail"] == expected_detail
    assert "network_unreachable" in last["stop_detail"]
    assert events_of(state_path, "candidate_skipped") == []
    assert identity not in [item["identity"] for item in last.get("skipped_candidate_failures", [])]
    for batch in events_of(state_path, "adaptive_batch_completed"):
        assert identity not in batch["candidate_identities"]
    assert identity not in last.get("candidate_identities", [])
    assert not [item for item in stopped if item["stop_reason"] == "source_exhausted"]
    return last


# ---------- 抖音 ----------


@pytest.mark.asyncio
async def test_douyin_request_transport_error_becomes_network_unreachable():
    transport = douyin_faults.Transport()
    ports = douyin_faults.entry.douyin_dependencies(douyin_faults.config)["ports"].client
    client = DouYinClient(
        headers={"User-Agent": "t129", "Cookie": ""}, playwright_page=None, cookie_dict={},
        ports=douyin_faults.replace(ports, make_async_client=transport),
    )

    with pytest.raises(DouyinRuntimeError) as caught:
        await client.request("GET", "https://www.douyin.com/aweme/v1/web/user/profile/other/")

    # 抖音请求层本来就不重试：仍只请求一次，只是异常改为运行级类型。
    assert caught.value.code == "network_unreachable"
    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert transport.calls == ["/aweme/v1/web/user/profile/other/"]


@pytest.mark.asyncio
async def test_douyin_creator_profile_network_failure_stops_run_without_skip(monkeypatch, tmp_path):
    state_path = douyin_faults.prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30")
    transport = douyin_faults.Transport()
    ports = douyin_faults.entry.douyin_dependencies(douyin_faults.config)["ports"].client
    real_client = DouYinClient(
        headers={"User-Agent": "t129", "Cookie": ""}, playwright_page=None, cookie_dict={},
        ports=douyin_faults.replace(ports, make_async_client=transport),
    )

    class Client:
        async def search_info_by_keyword(self, **kwargs):
            return {"status_code": 0, "data": [{"aweme_info": douyin_faults.image_aweme("first")}],
                    "has_more": 1, "extra": {"logid": "fresh-search-id"}}

        async def get_user_info(self, sec_uid):
            # 真实 client 的 get_user_info 依赖签名；这里直接走真实 request() 的传输层转换。
            return await real_client.request("GET", "https://www.douyin.com/aweme/v1/web/user/profile/other/")

    monkeypatch.setattr(douyin_core.asyncio, "sleep", AsyncMock(return_value=None))
    crawler = douyin_faults.DouYinCrawler()
    crawler.dy_client = Client()

    await crawler.search()  # 不再崩溃到 worker 顶层

    assert transport.calls == ["/aweme/v1/web/user/profile/other/"]
    assert_runtime_network_stop(state_path, "first", "network_unreachable")


# ---------- 知乎 ----------


def zhihu_client(transport) -> ZhiHuClient:
    return ZhiHuClient(
        ports=ZhihuClientPorts(transport, AsyncMock()),
        headers={"cookie": "d_c0=t129", "user-agent": "t129"},
        playwright_page=MagicMock(),
        cookie_dict={"d_c0": "t129"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [httpx.ConnectError, httpx.ReadTimeout])
async def test_zhihu_request_retries_then_raises_network_unreachable(monkeypatch, fault):
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ZhiHuClient.request.retry, "sleep", sleep)
    transport = zhihu_faults.Transport(fault)

    with pytest.raises(ZhihuRuntimeError) as caught:
        await zhihu_client(transport).request("GET", "https://www.zhihu.com/api/v4/search_v3?q=t129")

    # 重试预算不变（3 次 / 1 秒），转换只发生在重试用尽之后。
    assert transport.calls == ["/api/v4/search_v3"] * 3
    assert sleeps == [1, 1]
    assert caught.value.code == "network_unreachable"
    assert isinstance(caught.value.__cause__, fault)


@pytest.mark.asyncio
async def test_zhihu_detail_network_failure_stops_run_without_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(ZhiHuClient.request.retry, "sleep", AsyncMock(return_value=None))
    crawler, state_path = zhihu_faults.prepare(monkeypatch, tmp_path)
    excerpt = zhihu_faults.answer("needs-detail")
    excerpt.content_detail_status = "search_only"
    excerpt.content_detail_source = ""
    excerpt.image_list = []
    crawler.zhihu_client.get_note_by_keyword.side_effect = [
        zhihu_faults.search_page([excerpt]), zhihu_faults.END,
    ]
    transport = zhihu_faults.Transport(httpx.ReadTimeout)
    real_client = zhihu_client(transport)

    async def get_answer_info(question_id, answer_id):
        return await real_client.request("GET", f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}")

    crawler.zhihu_client.get_answer_info.side_effect = get_answer_info

    await crawler.search()  # 不再崩溃到 worker 顶层

    assert transport.calls == ["/question/question-1/answer/needs-detail"] * 3
    assert_runtime_network_stop(state_path, "needs-detail", "network_unreachable")


@pytest.mark.asyncio
async def test_zhihu_search_network_failure_is_runtime_failed(monkeypatch, tmp_path):
    crawler, state_path = zhihu_faults.prepare(monkeypatch, tmp_path)
    crawler.zhihu_client.get_note_by_keyword.side_effect = ZhihuRuntimeError(
        "zhihu network_unreachable: ConnectError", code="network_unreachable",
    )

    await crawler.search()

    assert_runtime_network_stop(state_path, "", "network_unreachable")


# ---------- 微博 ----------


@pytest.mark.asyncio
async def test_weibo_long_text_detail_network_failure_stops_run_without_skip(monkeypatch, tmp_path):
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(WeiboClient.request.retry, "sleep", sleep)
    monkeypatch.setattr(WeiboClient._get_note_info_direct.retry, "sleep", sleep)
    monkeypatch.setattr(weibo_faults.weibo_client.asyncio, "sleep", AsyncMock(return_value=None))
    transport = weibo_faults.Transport(
        {1: [weibo_faults.mblog("long", long=True), weibo_faults.mblog("short")]},
        detail_fault={"long"},
    )
    crawler, state_path = weibo_faults.prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    # 详情重试预算不变（3 次 / 1 秒），用尽后停止本轮；后面的候选不再处理。
    assert transport.calls.count("/detail/long") == 3
    assert sleeps.count(1) == 2
    assert_runtime_network_stop(state_path, "long", "network_unreachable")
    crawler.update_weibo_note.assert_not_awaited()


@pytest.mark.asyncio
async def test_weibo_search_network_failure_keeps_retry_budget(monkeypatch, tmp_path):
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(WeiboClient.request.retry, "sleep", sleep)
    monkeypatch.setattr(WeiboClient._get_note_info_direct.retry, "sleep", sleep)
    monkeypatch.setattr(weibo_faults.weibo_client.asyncio, "sleep", AsyncMock(return_value=None))
    transport = weibo_faults.Transport({}, search_fault=True)
    crawler, state_path = weibo_faults.prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(httpx.ConnectError):
        await crawler.search()

    assert transport.calls == ["/api/container/getIndex"] * 5
    assert sleeps == [3, 3, 3, 3]
    assert_runtime_network_stop(state_path, "", "network_unreachable")


@pytest.mark.asyncio
async def test_weibo_specified_note_network_failure_is_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(WeiboClient._get_note_info_direct.retry, "sleep", AsyncMock(return_value=None))
    transport = weibo_faults.Transport({}, detail_fault={"target"})
    crawler, _ = weibo_faults.prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(weibo_faults.PlatformRuntimeError) as caught:
        await crawler.get_note_info_task("target", asyncio.Semaphore(1))

    assert caught.value.code == "network_unreachable"


# ---------- B站 ----------


def test_bilibili_detail_network_failure_stops_run_without_skip(monkeypatch, tmp_path):
    network = bili115.Network(
        search=[bili115.search_data([bili115.search_item("101", "456"), bili115.search_item("102", "789")],
                                    num_pages=1, num_results=2)],
        detail={"101": URLError("离线网络中断")},
    )
    args, state_path, _ = bili115.prepare(monkeypatch, tmp_path, network)

    result = bili115.mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    # 详情重试预算不变（3 次），用尽后停止本轮；102 不再请求。
    assert network.calls["detail"] == bilibili_client.BILIBILI_DETAIL_MAX_ATTEMPTS == 3
    assert_runtime_network_stop(state_path, "101", "bilibili_article_detail_failed:network_unreachable")


def test_bilibili_follower_network_failure_stops_run_without_skip(monkeypatch, tmp_path):
    network = bili115.Network(
        search=[bili115.search_data([bili115.search_item("101", "456"), bili115.search_item("102", "789")],
                                    num_pages=1, num_results=2)],
        follower={"456": [URLError("离线网络中断")] * 3},
    )
    args, state_path, _ = bili115.prepare(monkeypatch, tmp_path, network)

    result = bili115.mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls["follower:456"] == 3
    assert network.calls["follower:789"] == 0
    assert_runtime_network_stop(state_path, "101", "bilibili_relation_stat_failed:network_unreachable")


def test_bilibili_search_network_failure_detail_names_network_unreachable(monkeypatch, tmp_path):
    network = bili115.Network(search=[URLError("断连")] * 3)
    args, state_path, _ = bili115.prepare(monkeypatch, tmp_path, network)

    result = bili115.mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    assert result["ok"] is False
    assert network.calls["search"] == 3
    assert_runtime_network_stop(state_path, "", "bilibili_article_search_failed:network_unreachable")


def test_bilibili_detail_bad_json_stays_candidate_skip(monkeypatch, tmp_path):
    class BadJson(bili115.Network):
        def __call__(self, request, timeout):
            if "/x/article/view" in request.full_url and "id=101" in request.full_url:
                self.calls["detail"] += 1
                response = bili115.Response({}, request.full_url)
                response.read = lambda: b"not-json"
                return response
            return super().__call__(request, timeout)

    network = BadJson(
        search=[bili115.search_data([bili115.search_item("101", "456")], num_pages=1, num_results=1),
                bili115.search_data([], num_pages=1, num_results=1)],
    )
    args, state_path, _ = bili115.prepare(monkeypatch, tmp_path, network)

    bili115.mediacrawler_crawl.run_bilibili_article_search(args, tmp_path / "batch")

    # 解码失败不是网络整体故障：仍是可重试的候选错误，按候选跳过。
    assert network.calls["detail"] == 3
    assert [item["identity"] for item in events_of(state_path, "candidate_skipped")] == ["101"]


# ---------- runner 分类 ----------


@pytest.mark.parametrize("platform, stop_detail", [
    ("douyin", "network_unreachable"),
    ("zhihu", "network_unreachable"),
    ("weibo", "network_unreachable"),
    ("bilibili", "bilibili_article_detail_failed:network_unreachable"),
    ("bilibili", "bilibili_relation_stat_failed:network_unreachable"),
])
def test_runner_classifies_network_unreachable_child_summary_as_network_timeout(platform, stop_detail):
    summary = {
        "records": [{"platform": platform, "failure_classification": {
            "status": "retry_wait", "failure_type": "runtime_failed", "retryable": True,
            "wait_seconds": 600, "reason": "nonzero_exit_without_specific_classification",
        }}],
        "runtime_blocker": {
            "platform": platform, "status": "blocked", "failure_type": "runtime_failed",
            "stop_reason": "runtime_failed", "reason": stop_detail, "retryable": False,
            "source": "adaptive_search_stopped",
        },
        "import_completion_met": False,
        "formal_validation": {"stop_reason": "runtime_failed"},
    }

    classification = classify_attempt(exit_code=1, stderr="", meta={"platform": platform}, summary=summary)

    assert classification["failure_type"] == "network_timeout"
    assert classification["status"] == "retry_wait"
    assert classification["retryable"] is True
    assert classification["wait_seconds"] == 900
    assert classification["reason"] == stop_detail


def test_runner_classifies_network_unreachable_text_as_network_timeout():
    classification = classify_attempt(
        exit_code=1,
        stderr="PlatformRuntimeError: douyin network_unreachable: ConnectError",
        meta={"platform": "douyin"},
    )

    assert (classification["failure_type"], classification["status"], classification["wait_seconds"]) == (
        "network_timeout", "retry_wait", 900,
    )
