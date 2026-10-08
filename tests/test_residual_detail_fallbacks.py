"""TripPostCollect tests for residual detail fallbacks."""

from __future__ import annotations

import asyncio

import pytest

# T14：原经旧桥 E 的重导出取用；四个函数本就是根实现（E 中为 `from ... import ... as ...`）。
from trippostcollect.platforms.douyin.parser import _douyin_detail_urls, _find_douyin_detail
from trippostcollect.platforms.weibo.client import _weibo_detail_api_url
from trippostcollect.platforms.weibo.parser import _find_weibo_detail


def test_douyin_repair_fallback_tries_note_before_video() -> None:
    assert _douyin_detail_urls("123") == (
        "https://www.douyin.com/note/123",
        "https://www.douyin.com/video/123",
    )


def test_douyin_nested_detail_rejects_id_only_shell() -> None:
    assert _find_douyin_detail({"aweme_detail": {"aweme_id": "123"}}, "123") is None

    detail = {"aweme_id": "123", "desc": "完整正文", "images": [{"uri": "asset"}]}
    assert _find_douyin_detail({"route": {"aweme_detail": detail}}, "123") == detail
    assert _find_douyin_detail({"aweme_detail": detail}, "different") is None


def test_weibo_nested_detail_is_bound_to_requested_id() -> None:
    target = {"idstr": "456", "text": "完整正文", "pics": [{"pid": "p1"}]}
    payload = {
        "status": {"id": "wrong", "text": "其他微博"},
        "page": {"mblog": target},
    }

    assert _find_weibo_detail(payload, "456") == target
    assert _find_weibo_detail(payload, "wrong")["text"] == "其他微博"
    assert _find_weibo_detail({"id": "456"}, "456") is None


def test_weibo_browser_api_is_bound_to_requested_id() -> None:
    assert _weibo_detail_api_url("456") == "https://m.weibo.cn/statuses/show?id=456"


def test_weibo_repair_hook_recovers_exact_browser_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trippostcollect.application import events
    from trippostcollect.platforms import entry
    from trippostcollect.platforms.weibo.client import WeiboClient
    from trippostcollect.platforms.weibo.models import DataFetchError

    # T14：原经旧桥 E 锁存修复开关并由 fork 工厂构造；改由正式 worker 的 install_hooks 与
    # load_crawler 完成同一装配。用例结束时还原 install_hooks 写入的模块级开关。
    for name in ("_weibo_post_repair", "_douyin_browser_detail_fallback", "_xhs_repair"):
        monkeypatch.setattr(entry, name, getattr(entry, name))
    monkeypatch.setattr(events, "_batch_publisher", events._batch_publisher)

    class FakePage:
        def __init__(self) -> None:
            self.goto_urls: list[str] = []

        async def goto(self, url: str, **_: object) -> None:
            self.goto_urls.append(url)

        async def wait_for_timeout(self, _: int) -> None:
            pass

        async def evaluate(self, _: str, note_id: str) -> dict[str, str]:
            return {"idstr": note_id, "text": "浏览器详情正文"}

    class FakeClient(WeiboClient):
        async def _get_note_info_direct(self, _: str) -> dict[str, object]:
            raise DataFetchError("missing $render_data")

    monkeypatch.setenv("TRIPPOSTCOLLECT_POST_REPAIR", "1")

    entry.install_hooks()
    crawler = entry.load_crawler("wb")()
    assert crawler.ports.post_repair is True
    client = FakeClient(
        headers={}, playwright_page=FakePage(), cookie_dict={},
        ports=crawler.ports.client, post_repair=crawler.ports.post_repair,
    )
    result = asyncio.run(client.get_note_info_by_id("456"))

    assert result == {"mblog": {"idstr": "456", "text": "浏览器详情正文", "id": "456"}}
    assert client.playwright_page.goto_urls == ["https://m.weibo.cn/detail/456"]
