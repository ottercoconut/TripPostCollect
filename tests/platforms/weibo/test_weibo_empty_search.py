from unittest.mock import MagicMock

import pytest

from support.weibo_adapter import client_ports
from trippostcollect.platforms.weibo.client import WeiboClient


class _Response:
    def json(self):
        return {
            "ok": 0,
            "msg": "这里还没有内容",
            "data": {"cards": []},
        }


class _AsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        return _Response()


@pytest.mark.asyncio
async def test_search_empty_message_is_returned_as_empty_page(monkeypatch):
    crawler_client = WeiboClient(
        ports=client_ports(make_async_client=lambda **kwargs: _AsyncClient()),
        headers={},
        playwright_page=MagicMock(),
        cookie_dict={},
    )

    result = await crawler_client.request(
        method="GET",
        url="https://m.weibo.cn/api/container/getIndex",
        allow_empty_search=True,
    )

    assert result == {"cards": []}
