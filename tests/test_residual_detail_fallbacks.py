"""TripPostCollect tests for residual detail fallbacks."""

from __future__ import annotations

import asyncio

import pytest

from scripts import mediacrawler_export_entrypoint as entrypoint
from scripts.mediacrawler_export_entrypoint import (
    _douyin_detail_urls,
    _find_douyin_detail,
    _find_weibo_detail,
    _weibo_detail_api_url,
)


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
    from test_adapter_t05_bridge import bridge_types
    from trippostcollect.platforms.weibo.client import WeiboClient
    from trippostcollect.platforms.weibo.models import DataFetchError

    factory, _, _ = bridge_types(monkeypatch)

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

    entrypoint.install_weibo_browser_detail_fallback()
    crawler = factory.create_crawler("wb")
    client = FakeClient(
        headers={}, playwright_page=FakePage(), cookie_dict={},
        ports=crawler.ports.client, post_repair=crawler.ports.post_repair,
    )
    result = asyncio.run(client.get_note_info_by_id("456"))

    assert result == {"mblog": {"idstr": "456", "text": "浏览器详情正文", "id": "456"}}
    assert client.playwright_page.goto_urls == ["https://m.weibo.cn/detail/456"]
