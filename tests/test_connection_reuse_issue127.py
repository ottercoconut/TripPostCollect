"""#127：抖音、微博、知乎在同一平台客户端内复用 httpx.AsyncClient。

不访问网络、不打开任何 socket、不启动浏览器。网络边界用 fake、``httpx.MockTransport``，或接到真实
httpcore 连接池的内存网络后端（连接复用与失效恢复要经过真实连接池，fake 证明不了）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from platforms.douyin import test_douyin_offline_faults as douyin_faults
from platforms.weibo import test_weibo_offline_faults as weibo_faults
from platforms.zhihu import test_zhihu_offline_faults as zhihu_faults
from test_network_errors_issue129 import assert_runtime_network_stop, events_of
from trippostcollect.platforms import entry
from trippostcollect.platforms.douyin import core as douyin_core
from trippostcollect.platforms.douyin.client import DouYinClient
from trippostcollect.platforms.douyin.core import DouYinCrawler
from trippostcollect.platforms.weibo.client import WeiboClient
from trippostcollect.platforms.weibo.core import WeiboCrawler
from trippostcollect.platforms.zhihu.client import ZhiHuClient
from trippostcollect.platforms.zhihu.core import ZhihuCrawler
from trippostcollect.runtime import worker
from trippostcollect.runtime.http import make_reused_async_client


class FakeHttpClient:
    def __init__(self, platform: str):
        self.platform = platform
        self.requests: list[tuple[str, str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.close_calls = 0

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        request = httpx.Request(method, url)
        if self.platform == "douyin":
            payload = {"status_code": 0, "data": {}}
        elif self.platform == "weibo":
            payload = {"ok": 1, "data": {"ok": True}}
        else:
            payload = {"data": {"ok": True}}
        return httpx.Response(200, json=payload, request=request)

    async def get(self, url: str, **kwargs):
        self.gets.append((url, kwargs))
        return httpx.Response(
            302,
            headers={"Location": "https://www.douyin.com/note/resolved"},
            request=httpx.Request("GET", url),
        )

    async def aclose(self):
        self.close_calls += 1


class FakeHttpFactory:
    def __init__(self, platform: str):
        self.platform = platform
        self.calls: list[dict] = []
        self.clients: list[FakeHttpClient] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        client = FakeHttpClient(self.platform)
        self.clients.append(client)
        return client


def make_client(platform: str, factory: FakeHttpFactory):
    ports = SimpleNamespace(make_async_client=factory)
    kwargs = {
        "headers": {"User-Agent": "issue127", "cookie": "d_c0=issue127"},
        "playwright_page": None,
        "cookie_dict": {},
        "ports": ports,
        "proxy": "http://proxy.invalid",
    }
    if platform == "douyin":
        return DouYinClient(**kwargs), "https://www.douyin.com/test"
    if platform == "weibo":
        return WeiboClient(**kwargs), "https://m.weibo.cn/test"
    return ZhiHuClient(**kwargs), "https://www.zhihu.com/test"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_requests_reuse_one_factory_client_and_close_is_idempotent(platform):
    factory = FakeHttpFactory(platform)
    client, url = make_client(platform, factory)

    await client.request("GET", url)
    await client.request("GET", url)

    assert len(factory.calls) == 1
    assert factory.calls[0] == {"proxy": "http://proxy.invalid"}
    assert len(factory.clients) == 1
    assert len(factory.clients[0].requests) == 2
    assert all(item[1] == url for item in factory.clients[0].requests)

    await client.aclose()
    await client.aclose()
    assert factory.clients[0].close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_concurrent_first_requests_construct_only_one_client(platform):
    factory = FakeHttpFactory(platform)
    client, url = make_client(platform, factory)

    await asyncio.gather(*(client.request("GET", url) for _ in range(8)))

    assert len(factory.calls) == 1
    assert len(factory.clients[0].requests) == 8


@pytest.mark.asyncio
async def test_douyin_short_url_disables_redirects_only_for_that_request():
    factory = FakeHttpFactory("douyin")
    client, url = make_client("douyin", factory)

    await client.request("GET", url)
    resolved = await client.resolve_short_url("https://v.douyin.com/short")
    await client.request("GET", url)

    assert resolved == "https://www.douyin.com/note/resolved"
    assert factory.calls == [{"proxy": "http://proxy.invalid"}]
    http_client = factory.clients[0]
    assert http_client.gets == [(
        "https://v.douyin.com/short",
        {"timeout": 10, "follow_redirects": False},
    )]
    assert len(http_client.requests) == 2
    assert all("follow_redirects" not in kwargs for _, _, kwargs in http_client.requests)


class CloseRecorder:
    def __init__(self, *, error: Exception | None = None):
        self.close_calls = 0
        self.calls: list[dict] = []
        self.error = error

    async def aclose(self):
        self.close_calls += 1
        if self.error:
            raise self.error

    async def close(self):
        self.close_calls += 1

    async def cleanup(self, **kwargs):
        self.close_calls += 1
        self.calls.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("crawler_type", "attribute"), [
    (DouYinCrawler, "dy_client"),
    (WeiboCrawler, "wb_client"),
    (ZhihuCrawler, "zhihu_client"),
])
async def test_crawler_close_closes_http_client_and_browser(crawler_type, attribute):
    crawler = object.__new__(crawler_type)
    http_client = CloseRecorder()
    browser = CloseRecorder()
    setattr(crawler, attribute, http_client)
    crawler.cdp_manager = None
    crawler.browser_context = browser

    await crawler.close()

    assert http_client.close_calls == 1
    assert browser.close_calls == 1


@pytest.mark.asyncio
async def test_crawler_close_preserves_browser_cleanup_when_http_close_fails():
    crawler = object.__new__(DouYinCrawler)
    crawler.dy_client = CloseRecorder(error=RuntimeError("close failed"))
    crawler.cdp_manager = None
    crawler.browser_context = CloseRecorder()

    with pytest.raises(RuntimeError, match="close failed"):
        await crawler.close()

    assert crawler.browser_context.close_calls == 1


@pytest.mark.asyncio
async def test_worker_cleanup_closes_all_general_http_clients_after_forced_cdp_cleanup():
    clients = [CloseRecorder() for _ in range(3)]
    crawler = SimpleNamespace(
        dy_client=clients[0],
        wb_client=clients[1],
        zhihu_client=clients[2],
        cdp_manager=CloseRecorder(),
        browser_context=CloseRecorder(),
    )

    await worker.async_cleanup(crawler, "dy")
    await worker.async_cleanup(crawler, "dy")

    assert [client.close_calls for client in clients] == [2, 2, 2]
    assert crawler.cdp_manager.close_calls == 2
    assert crawler.cdp_manager.calls == [{"force": True}, {"force": True}]
    assert crawler.browser_context.close_calls == 0


@pytest.mark.asyncio
async def test_worker_http_close_failure_does_not_skip_browser_cleanup():
    crawler = SimpleNamespace(
        dy_client=CloseRecorder(error=RuntimeError("close failed")),
        wb_client=None,
        zhihu_client=None,
        cdp_manager=None,
        browser_context=CloseRecorder(),
    )

    await worker.async_cleanup(crawler, "dy")

    assert crawler.browser_context.close_calls == 1


class OrderRecorder:
    def __init__(self, order: list[str], name: str, error: BaseException | None = None):
        self.order, self.name, self.error = order, name, error

    async def aclose(self):
        self.order.append(self.name)
        if self.error:
            raise self.error

    async def cleanup(self, **kwargs):
        self.order.append(self.name)


@pytest.mark.asyncio
async def test_worker_cleans_browser_before_a_cancelled_http_close():
    # 被取消的 client 关闭不属于 Exception，浏览器清理必须已经完成（否则 Chrome 留到清理超时）。
    order: list[str] = []
    crawler = SimpleNamespace(
        dy_client=OrderRecorder(order, "http", asyncio.CancelledError()),
        cdp_manager=OrderRecorder(order, "browser"),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.async_cleanup(crawler, "dy")

    assert order == ["browser", "http"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crawler_type", [DouYinCrawler, WeiboCrawler, ZhihuCrawler])
async def test_crawler_close_before_client_creation_still_closes_browser(crawler_type):
    crawler = object.__new__(crawler_type)
    crawler.cdp_manager = None
    crawler.browser_context = CloseRecorder()

    await crawler.close()

    assert crawler.browser_context.close_calls == 1


def worker_crawler(order: list[str]):
    """真实平台 client 与假浏览器：worker 生命周期结束后 HTTP client 必须已关闭。"""
    factory = FakeHttpFactory("douyin")
    client, url = make_client("douyin", factory)
    crawler = SimpleNamespace(dy_client=client, cdp_manager=OrderRecorder(order, "browser"))
    return crawler, client, url, factory


def test_worker_signal_path_closes_http_client_and_browser():
    order: list[str] = []
    crawler, client, url, factory = worker_crawler(order)

    async def main():
        await client.request("GET", url)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(30)

    worker.run(main, lambda: worker.async_cleanup(crawler, "dy"), cleanup_timeout_seconds=5)

    assert order == ["browser"]
    assert factory.clients[0].close_calls == 1


def test_worker_exception_path_closes_http_client_and_browser():
    order: list[str] = []
    crawler, client, url, factory = worker_crawler(order)

    async def main():
        await client.request("GET", url)
        raise ValueError("业务异常")

    with pytest.raises(ValueError, match="业务异常"):
        worker.run(main, lambda: worker.async_cleanup(crawler, "dy"), cleanup_timeout_seconds=5)

    assert order == ["browser"]
    assert factory.clients[0].close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_request_after_close_is_run_level_and_never_reconnects(platform):
    # 清理后残留的调用不得悄悄新建 client；运行级错误也不进重试、不按候选跳过。
    factory = FakeHttpFactory(platform)
    client, url = make_client(platform, factory)
    await client.request("GET", url)
    await client.aclose()

    with pytest.raises(Exception) as caught:
        await client.request("GET", url)

    assert type(caught.value).__name__ == "PlatformRuntimeError"
    assert caught.value.code == "http_client_closed"
    assert len(factory.calls) == 1
    assert len(factory.clients[0].requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_close_before_first_request_creates_nothing(platform):
    factory = FakeHttpFactory(platform)
    client, _ = make_client(platform, factory)

    await client.aclose()
    await client.aclose()

    assert factory.calls == []


def assembled_ports(platform: str, transport: httpx.MockTransport):
    """取入口真实装配的工厂，只在最底层换 transport，证明 cookie jar 策略确实接到了平台上。"""
    if platform == "douyin":
        ports = entry.douyin_dependencies(douyin_faults.config)["ports"].client
    elif platform == "weibo":
        ports = entry.weibo_dependencies(weibo_faults.config)[1].client
    else:
        ports = entry._zhihu_dependencies(zhihu_faults.config)[1].client_factory.keywords["ports"]
    real_factory = ports.make_async_client
    return replace(ports, make_async_client=lambda **kwargs: real_factory(**kwargs, transport=transport))


SITES = {
    "douyin": ("https://www.douyin.com/test", ".douyin.com", "https://p3.douyin.com/a.jpg"),
    "weibo": ("https://m.weibo.cn/test", ".weibo.cn", "https://img.weibo.cn/a.jpg"),
    "zhihu": ("https://www.zhihu.com/test", ".zhihu.com", "https://pic1.zhihu.com/a.jpg"),
}


async def fetch_image(platform: str, client, url: str):
    if platform == "douyin":
        return await client.get_aweme_media(url)
    if platform == "weibo":
        return await client.get_note_image(url)
    return await client.get_content_image(url, referer="https://www.zhihu.com/question/1")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_set_cookie_never_accumulates_on_reused_client(platform):
    # 响应下发同域 Cookie：显式 Cookie 头保持原样，未带 Cookie 头的图片请求也不能被 jar 附上。
    api_url, domain, image_url = SITES[platform]
    seen: list[str | None] = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            headers=[("set-cookie", f"rotated=1; Domain={domain}; Path=/")],
            json={"ok": 1, "status_code": 0, "data": {"ok": True}},
        )

    client, _ = make_client(platform, FakeHttpFactory(platform))
    client.ports = assembled_ports(platform, httpx.MockTransport(handler))
    client.proxy = None

    await client.request("GET", api_url, headers={"Cookie": "session=first"})
    await client.request("GET", api_url, headers={"Cookie": "session=second"})
    await fetch_image(platform, client, image_url)
    await client.aclose()

    assert seen == ["session=first", "session=second", None]


@pytest.mark.asyncio
async def test_douyin_redirect_option_is_per_request_on_the_real_client():
    def handler(request):
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"Location": "https://p3.douyin.com/final.jpg"})
        return httpx.Response(200, content=b"image")

    client, _ = make_client("douyin", FakeHttpFactory("douyin"))
    client.ports = assembled_ports("douyin", httpx.MockTransport(handler))
    client.proxy = None

    assert await client.resolve_short_url("https://v.douyin.com/moved") == "https://p3.douyin.com/final.jpg"
    assert await client.get_aweme_media("https://p3.douyin.com/moved") == b"image"
    assert await client.resolve_short_url("https://v.douyin.com/moved") == "https://p3.douyin.com/final.jpg"
    await client.aclose()


BODY = b'{"ok": 1, "status_code": 0, "data": {"ok": true}}'
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(BODY), BODY)


class MemoryHttpStream(httpcore.AsyncNetworkStream):
    """一条内存连接的客户端一端：请求头到齐时，服务端按模式写入响应或断开。"""

    def __init__(self, server: "MemoryHttpServer", number: int):
        self.server = server
        self.number = number
        self.received = b""
        self.pending = b""  # 服务端已发出、客户端尚未读走的字节
        self.served = 0
        self.peer_closed = False

    async def write(self, buffer, timeout=None):
        if not buffer:
            return  # h11 结束无请求体的消息时写空字节，不算发送
        if self.peer_closed:
            self.server.dead_writes += 1  # 对端已断开：字节丢失，随后的 read 读到 EOF
            return
        self.received += buffer
        while b"\r\n\r\n" in self.received:
            _, _, self.received = self.received.partition(b"\r\n\r\n")
            self.server.requests.append(self.number)
            if self.server.mode == "drop_reused" and self.number == 1 and self.served == 1:
                self.peer_closed = True
                return
            self.pending += RESPONSE
            self.served += 1
            if self.server.mode == "close_idle":
                self.peer_closed = True

    async def read(self, max_bytes, timeout=None):
        if not self.pending and not self.peer_closed:
            raise AssertionError("无数据且连接未断开时读取：真实网络上会一直阻塞")
        data, self.pending = self.pending[:max_bytes], self.pending[max_bytes:]
        return data  # 对端已断开且无数据时为 b""，即 EOF

    async def aclose(self):
        pass

    def get_extra_info(self, info):
        # 与 httpcore 自带的 socket 流同义：有待读数据或对端已断开时可读，空闲连接的失效由 httpcore 据此判定。
        if info == "is_readable":
            return bool(self.pending) or self.peer_closed
        return None


class MemoryHttpServer(httpcore.AsyncNetworkBackend):
    """内存 HTTP/1.1 服务，同时是 httpcore 的网络后端；``requests`` 记录每个请求落在第几条连接上。

    macOS component lane 禁止一切 socket（含 Unix socket），所以只替换最底层的字节流：连接池与 h11
    解析都是真实代码，而 ``httpx.MockTransport`` 会绕过连接池，测不到复用。

    ``keep`` 连接一直可用；``close_idle`` 答复后服务端随即断开空闲连接；
    ``drop_reused`` 连接 1 上的第二个请求不答复就断开，即复用到已失效连接。
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.connections = 0
        self.requests: list[int] = []
        self.dead_writes = 0

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connections += 1
        return MemoryHttpStream(self, self.connections)

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)

    def client(self, platform: str):
        factory_calls: list[dict] = []

        def factory(**kwargs):
            factory_calls.append(kwargs)
            transport = httpx.AsyncHTTPTransport()
            # httpx 的 transport 没有公开 network_backend 入口，只能换连接池的私有属性；池配置保持 httpx 默认。
            transport._pool._network_backend = self
            return make_reused_async_client(disable_ssl_verify=False, transport=transport, **kwargs)

        client, _ = make_client(platform, FakeHttpFactory(platform))
        client.ports = SimpleNamespace(make_async_client=factory)
        client.proxy = None
        return client, factory_calls


LOCAL_URL = "http://local.test/api"


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(WeiboClient.request.retry, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(ZhiHuClient.request.retry, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_requests_share_one_real_connection(platform):
    server = MemoryHttpServer("keep")
    client, factory_calls = server.client(platform)
    for _ in range(3):
        await client.request("GET", LOCAL_URL)
    await client.aclose()

    assert server.requests == [1, 1, 1]
    assert len(factory_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["douyin", "weibo", "zhihu"])
async def test_idle_connection_closed_by_server_is_replaced_without_error(platform):
    # 空闲连接被服务端断开是复用后最常见的失效：连接池应换新连接，抖音也不能因此停轮。
    server = MemoryHttpServer("close_idle")
    client, factory_calls = server.client(platform)
    await client.request("GET", LOCAL_URL)
    await client.request("GET", LOCAL_URL)
    await client.aclose()

    assert server.requests == [1, 2]
    assert server.dead_writes == 0  # 由连接池换新，不是写到失效连接后靠重试恢复
    assert len(factory_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["weibo", "zhihu"])
async def test_retry_after_dead_reused_connection_recovers_on_new_connection(platform, no_retry_wait):
    server = MemoryHttpServer("drop_reused")
    client, factory_calls = server.client(platform)
    await client.request("GET", LOCAL_URL)
    await client.request("GET", LOCAL_URL)
    await client.aclose()

    assert server.requests == [1, 1, 2]
    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_douyin_dead_reused_connection_is_run_level_and_not_sticky():
    # 抖音请求层不重试（#129）：失效连接按运行级停止；连接池已弃用它，后续请求走新连接。
    server = MemoryHttpServer("drop_reused")
    client, factory_calls = server.client("douyin")
    await client.request("GET", LOCAL_URL)
    with pytest.raises(douyin_faults.PlatformRuntimeError) as caught:
        await client.request("GET", LOCAL_URL)
    await client.request("GET", LOCAL_URL)
    await client.aclose()

    assert caught.value.code == "network_unreachable"
    assert isinstance(caught.value.__cause__, httpx.RemoteProtocolError)
    assert server.requests == [1, 1, 2]
    assert len(factory_calls) == 1


def dropped(method: str, url: str) -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError("Server disconnected", request=httpx.Request(method, url))


class WeiboPageTwoDrops(weibo_faults.Transport):
    def __init__(self, pages):
        super().__init__(pages)
        self.factory_calls = 0

    def __call__(self, **kwargs):
        self.factory_calls += 1
        return self

    async def request(self, method, url, **kwargs):
        parsed = httpx.URL(url)
        if parsed.path == "/api/container/getIndex" and parsed.params["page"] == "2":
            self.calls.append(parsed.path)
            raise dropped(method, url)
        return await super().request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_weibo_dead_connection_keeps_checkpoint_at_failed_page(monkeypatch, tmp_path):
    monkeypatch.setattr(WeiboClient.request.retry, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(WeiboClient._get_note_info_direct.retry, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(weibo_faults.weibo_client.asyncio, "sleep", AsyncMock(return_value=None))
    transport = WeiboPageTwoDrops({1: [weibo_faults.mblog("first")]})
    crawler, state_path = weibo_faults.prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(httpx.RemoteProtocolError):
        await crawler.search()

    assert transport.factory_calls == 1
    assert transport.calls.count("/api/container/getIndex") == 6  # 第 1 页一次，第 2 页 5 次重试
    last = assert_runtime_network_stop(state_path, "second", "network_unreachable")
    assert (last["resume_page"], last["batch_complete"]) == (2, False)


class ZhihuPageTwoDrops(zhihu_faults.SearchHttp):
    async def request(self, method, url, **kwargs):
        parsed = httpx.URL(url)
        if parsed.params.get("offset", "0") != "0":
            self.calls.append((parsed.path, 20))
            raise dropped(method, url)
        return await super().request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_zhihu_dead_connection_keeps_checkpoint_at_failed_page(monkeypatch, tmp_path):
    monkeypatch.setattr(ZhiHuClient.request.retry, "sleep", AsyncMock(return_value=None))
    crawler, state_path, _ = zhihu_faults.prepare_real_client(
        monkeypatch, tmp_path, [], [],
    )
    http = ZhihuPageTwoDrops([(200, {"data": [zhihu_faults.raw_answer("a1")], "paging": {"is_end": False}})])
    crawler.zhihu_client = zhihu_faults.search_client_for(http)

    await crawler.search()

    assert http.calls == [("/api/v4/search_v3", 0)] + [("/api/v4/search_v3", 20)] * 3
    last = assert_runtime_network_stop(state_path, "a2", "network_unreachable")
    assert (last["resume_page"], last["batch_complete"]) == (2, False)


@pytest.mark.asyncio
async def test_douyin_dead_connection_does_not_finish_the_batch(monkeypatch, tmp_path):
    state_path = douyin_faults.prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30")
    monkeypatch.setattr(douyin_core.asyncio, "sleep", AsyncMock(return_value=None))
    factory_calls: list[dict] = []

    class Http:
        async def request(self, method, url, **kwargs):
            raise dropped(method, url)

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return Http()

    ports = entry.douyin_dependencies(douyin_faults.config)["ports"].client
    real_client = DouYinClient(
        headers={"User-Agent": "t127", "Cookie": ""}, playwright_page=None, cookie_dict={},
        ports=replace(ports, make_async_client=factory),
    )

    class Client:
        async def search_info_by_keyword(self, **kwargs):
            return {"status_code": 0, "data": [{"aweme_info": douyin_faults.image_aweme("first")}],
                    "has_more": 1, "extra": {"logid": "fresh-search-id"}}

        async def get_user_info(self, sec_uid):
            return await real_client.request("GET", "https://www.douyin.com/aweme/v1/web/user/profile/other/")

    crawler = douyin_faults.DouYinCrawler()
    crawler.dy_client = Client()

    await crawler.search()

    assert len(factory_calls) == 1
    last = assert_runtime_network_stop(state_path, "first", "network_unreachable")
    assert (last["resume_offset"], last["batch_complete"]) == (0, False)
    assert [batch["batch_complete"] for batch in events_of(state_path, "adaptive_batch_completed")] in ([], [False])
