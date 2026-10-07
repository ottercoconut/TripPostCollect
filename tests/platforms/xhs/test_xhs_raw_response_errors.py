# -*- coding: utf-8 -*-

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from tenacity import Future, RetryError

from trippostcollect.platforms.xhs.client import (
    XiaoHongShuClient,
    is_recoverable_xhs_transport_failure,
)
from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    PlatformRuntimeError,
)
from trippostcollect.platforms.xhs.manual_wait import (
    XHSManualWaitBudget,
    XHSManualWaitBudgetExhausted,
)

from .support import client_ports


class FakeAsyncClient:
    def __init__(self, request_impl):
        self.request_impl = request_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, *args, **kwargs):
        return await self.request_impl(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self.request_impl("GET", *args, **kwargs)


def make_client(*, manual_wait_budget=None):
    client = XiaoHongShuClient(
        headers={"Cookie": "web_session=test; a1=test"},
        playwright_page=Mock(),
        cookie_dict={},
        manual_wait_budget=manual_wait_budget,
        ports=client_ports,
    )
    return client


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "failure_code"),
    [(401, "login_required"), (403, "login_required"), (429, "rate_limited")],
)
async def test_raw_response_rejects_access_http_status(
    monkeypatch, status_code, failure_code
):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            text="blocked",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await make_client().request(
            "GET", "https://www.xiaohongshu.com/user/profile/test", return_response=True
        )

    assert exc_info.value.code == failure_code
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_exception", "failure_code", "return_response"),
    [
        (300011, PlatformRuntimeError, "platform_security_limit_300011", True),
        ("300011", PlatformRuntimeError, "platform_security_limit_300011", False),
        (300012, IPBlockError, None, True),
        ("300012", IPBlockError, None, False),
    ],
)
async def test_raw_response_rejects_known_business_block(
    monkeypatch, code, expected_exception, failure_code, return_response
):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": False, "code": code, "msg": "blocked"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(expected_exception) as exc_info:
        await make_client().request(
            "GET",
            "https://www.xiaohongshu.com/explore/test",
            return_response=return_response,
        )

    if failure_code is not None:
        assert exc_info.value.code == failure_code
    assert calls == 1


@pytest.mark.asyncio
async def test_raw_response_keeps_successful_html(monkeypatch):
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            200,
            text="<html>ok</html>",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    result = await make_client().request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )

    assert result == "<html>ok</html>"


@pytest.mark.asyncio
async def test_successful_json_keeps_raw_and_parsed_return_modes(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": True, "data": {"id": "test"}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )
    client = make_client()

    raw_result = await client.request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )
    parsed_result = await client.request(
        "GET", "https://edith.xiaohongshu.com/api/test"
    )

    assert json.loads(raw_result) == {"success": True, "data": {"id": "test"}}
    assert parsed_result == {"id": "test"}
    assert calls == 2


@pytest.mark.asyncio
async def test_api_captcha_popups_share_the_crawler_manual_budget(monkeypatch):
    clock = FakeClock()
    budget = XHSManualWaitBudget(
        limit_seconds=10.0,
        monotonic=clock.monotonic,
    )
    client = make_client(manual_wait_budget=budget)
    client.update_cookies = AsyncMock()
    verification_timeouts: list[float] = []

    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            461,
            headers={"Verifytype": "216", "Verifyuuid": "uuid"},
            request=httpx.Request(method, url),
        )

    async def verify(_page, **kwargs):
        verification_timeouts.append(kwargs["timeout_seconds"])
        clock.advance(4.0 if len(verification_timeouts) == 1 else 6.0)
        return {"status": "completed"}

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )
    monkeypatch.setattr(
        "trippostcollect.platforms.xhs.client.run_required_api_captcha_verification",
        verify,
    )
    raw_request = XiaoHongShuClient.request.__wrapped__.__wrapped__

    with pytest.raises(DataFetchError, match="retrying the original request"):
        await raw_request(client, "GET", "https://edith.xiaohongshu.com/api/test")

    assert budget.manual_elapsed_seconds == 4.0
    assert budget.remaining_seconds == 6.0

    with pytest.raises(XHSManualWaitBudgetExhausted):
        await raw_request(client, "GET", "https://edith.xiaohongshu.com/api/test")

    assert verification_timeouts == [10.0, 6.0]
    assert budget.manual_elapsed_seconds == 10.0
    assert client.update_cookies.await_count == 1


@pytest.mark.asyncio
async def test_api_captcha_error_after_budget_boundary_uses_fixed_terminal_reason(
    monkeypatch,
):
    clock = FakeClock()
    budget = XHSManualWaitBudget(
        limit_seconds=3.0,
        monotonic=clock.monotonic,
    )
    client = make_client(manual_wait_budget=budget)

    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            461,
            headers={"Verifytype": "216", "Verifyuuid": "uuid"},
            request=httpx.Request(method, url),
        )

    async def failed_verification(_page, **_kwargs):
        clock.advance(3.0)
        raise RuntimeError("xhs_api_captcha_page_unavailable")

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )
    monkeypatch.setattr(
        "trippostcollect.platforms.xhs.client.run_required_api_captcha_verification",
        failed_verification,
    )
    raw_request = XiaoHongShuClient.request.__wrapped__.__wrapped__

    with pytest.raises(
        XHSManualWaitBudgetExhausted,
        match="^xhs_manual_checkpoint_budget_exhausted$",
    ):
        await raw_request(client, "GET", "https://edith.xiaohongshu.com/api/test")


@pytest.mark.asyncio
async def test_html_detail_does_not_multiply_transport_retries(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(RetryError):
        await make_client().get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert calls == 3


@pytest.mark.asyncio
async def test_html_detail_still_retries_parse_failures():
    client = make_client()
    client.request = AsyncMock(return_value="<html>incomplete</html>")
    client._extractor.extract_note_detail_from_html = Mock(
        side_effect=ValueError("incomplete initial state")
    )

    with pytest.raises(RetryError):
        await client.get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert client.request.await_count == 3
    assert client._extractor.extract_note_detail_from_html.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_pong_returns_false_only_for_explicit_unauthenticated_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            status_code,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    assert await make_client().pong() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_pong_propagates_temporary_transport_outage(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.RequestError],
) -> None:
    async def request_impl(method, url, **kwargs):
        raise error_type("network unavailable", request=httpx.Request(method, url))

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(error_type, match="network unavailable"):
        await make_client().pong()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (429, "rate_limited"),
        (461, "verification_required"),
        (471, "verification_required"),
    ],
)
async def test_pong_does_not_turn_platform_checkpoint_into_logged_out(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            status_code,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await make_client().pong()

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_code", "error_type"),
    [(300011, PlatformRuntimeError), (300012, IPBlockError)],
)
async def test_pong_preserves_business_block_from_successful_http_response(
    monkeypatch: pytest.MonkeyPatch,
    body_code: int,
    error_type: type[Exception],
) -> None:
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": False, "code": body_code},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        client_ports,
        "make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(error_type):
        await make_client().pong()


def test_transport_classifier_unwraps_retry_error_without_broad_request_errors() -> None:
    request = httpx.Request("GET", "https://edith.xiaohongshu.com/api/test")
    retry_timeout = RetryError(
        Future.construct(
            3,
            httpx.ReadTimeout("offline", request=request),
            has_exception=True,
        )
    )

    assert is_recoverable_xhs_transport_failure(retry_timeout) is True
    assert is_recoverable_xhs_transport_failure(
        httpx.RemoteProtocolError("peer reset", request=request)
    ) is True
    assert is_recoverable_xhs_transport_failure(
        httpx.LocalProtocolError("invalid request", request=request)
    ) is False
    assert is_recoverable_xhs_transport_failure(DataFetchError("bad payload")) is False
    assert is_recoverable_xhs_transport_failure(
        PlatformRuntimeError("HTTP 429", code="rate_limited")
    ) is False


@pytest.mark.asyncio
async def test_pong_does_not_swallow_cancellation() -> None:
    client = make_client()
    client.query_self = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.pong()
