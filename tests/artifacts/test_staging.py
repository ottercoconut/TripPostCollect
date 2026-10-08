"""T14 自 fork 离线用例移植（按台账 target_file）：图片客户端 HTTP 分类、下载重试与暂存错误码。

来源 tools/MediaCrawler/tests 下 test_image_client_http_classification.py、test_image_download_retry.py、
test_image_staging_errors.py。原用例经 fork 薄转发（media_platform.* 客户端、tools.image_download_retry、
tools.image_manifest）调用根实现；这里直接导入根模块并沿用原别名，用例名、参数与断言逐条不变。
微博用例原经 fork config 装配，改用根配置对象（与 fork 默认值逐键相同，test_adapter_t12 守护）。
HTTP 只用 httpx.MockTransport，不访问网络。
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from PIL import Image
import pytest

from trippostcollect.application.worker_inputs import worker_config
from trippostcollect.artifacts import image_staging as image_manifest
from trippostcollect.platforms.douyin import client as douyin_client
from trippostcollect.platforms.weibo import client as weibo_client
from trippostcollect.platforms.xhs import client as xhs_client
from trippostcollect.platforms.xhs.errors import IPBlockError, PlatformRuntimeError
from trippostcollect.platforms.zhihu import client as zhihu_client
from trippostcollect.runtime import image_retry as image_download_retry
from trippostcollect.runtime.image_retry import ImageDownloadFetchError


def async_client_factory(status_code: int, content: bytes = b""):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, request=request)

    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_douyin_http_404_preserves_terminal_status(monkeypatch) -> None:
    from types import SimpleNamespace

    client = object.__new__(douyin_client.DouYinClient)
    # T06：HTTP 依赖已迁入显式端口，继续只验证本站原 404 分类。
    client.ports = SimpleNamespace(make_async_client=async_client_factory(404))
    client.proxy = None
    client.timeout = 1

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_aweme_media("https://example.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_weibo_http_404_preserves_terminal_status(monkeypatch) -> None:
    from trippostcollect.platforms.entry import weibo_dependencies

    # fork 微博重导出根客户端，HTTP 替身注入根端口的实际调用边界。
    monkeypatch.setattr("trippostcollect.runtime.http.make_async_client", async_client_factory(404))
    client = object.__new__(weibo_client.WeiboClient)
    client.ports = weibo_dependencies(worker_config())[1].client
    client.proxy = None
    client.timeout = 1
    client._image_agent_host = "https://i1.wp.com/"

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image("https://wx.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_xhs_http_404_preserves_terminal_status(monkeypatch) -> None:
    client = object.__new__(xhs_client.XiaoHongShuClient)
    # T09：HTTP 依赖已迁入显式端口，继续只验证本站原分类。
    client._ports = SimpleNamespace(make_async_client=async_client_factory(404))
    client.proxy = None
    client.timeout = 1

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_media("https://example.test/missing.jpg")

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "login_required"), (403, "login_required"), (429, "rate_limited")],
)
@pytest.mark.asyncio
async def test_xhs_html_response_preserves_run_level_http_status(
    monkeypatch,
    status,
    code,
) -> None:
    client = object.__new__(xhs_client.XiaoHongShuClient)
    # T09：HTTP 依赖已迁入显式端口，继续只验证本站原分类。
    client._ports = SimpleNamespace(make_async_client=async_client_factory(status))
    client.proxy = None
    client.timeout = 1

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await client.request(
            "GET",
            "https://www.xiaohongshu.com/user/profile/test",
            return_response=True,
        )

    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_xhs_html_response_preserves_ip_block_business_code(monkeypatch) -> None:
    payload = b'{"success":false,"code":300012,"msg":"blocked"}'
    client = object.__new__(xhs_client.XiaoHongShuClient)
    # T09：HTTP 依赖已迁入显式端口，继续只验证本站原分类。
    client._ports = SimpleNamespace(make_async_client=async_client_factory(200, payload))
    client.proxy = None
    client.timeout = 1
    client.IP_ERROR_CODE = 300012
    client.IP_ERROR_STR = "Network connection error, code 300012"

    with pytest.raises(IPBlockError):
        await client.request(
            "GET",
            "https://www.xiaohongshu.com/explore/test",
            return_response=True,
        )


@pytest.mark.asyncio
async def test_xhs_html_response_preserves_security_limit_business_code(
    monkeypatch,
) -> None:
    payload = b'{"success":false,"code":300011,"msg":"Account exception"}'
    client = object.__new__(xhs_client.XiaoHongShuClient)
    # T09：HTTP 依赖已迁入显式端口，继续只验证本站原分类。
    client._ports = SimpleNamespace(make_async_client=async_client_factory(200, payload))
    client.proxy = None
    client.timeout = 1
    client.IP_ERROR_CODE = 300012
    client.IP_ERROR_STR = "Network connection error, code 300012"

    with pytest.raises(PlatformRuntimeError) as exc_info:
        await client.request(
            "GET",
            "https://www.xiaohongshu.com/user/profile/test",
            return_response=True,
        )

    assert exc_info.value.code == "platform_security_limit_300011"


@pytest.mark.asyncio
async def test_zhihu_http_404_preserves_terminal_status(monkeypatch) -> None:
    from types import SimpleNamespace

    client = object.__new__(zhihu_client.ZhiHuClient)
    # T07：fork 类重导出根实现，HTTP 替身经实例端口注入。
    client.ports = SimpleNamespace(make_async_client=async_client_factory(404))
    client.proxy = None
    client.timeout = 1
    client.default_headers = {}

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_content_image(
            "https://example.test/missing.jpg",
            referer="https://www.zhihu.com/question/1",
        )

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_zhihu_oversized_response_is_terminal(monkeypatch) -> None:
    from types import SimpleNamespace
    from trippostcollect.platforms.zhihu import client as root_zhihu_client

    monkeypatch.setattr(root_zhihu_client, "IMAGE_DOWNLOAD_MAX_BYTES", 3)
    client = object.__new__(zhihu_client.ZhiHuClient)
    # T07：只改 mock 接缝，大小上限与错误断言保持原样。
    client.ports = SimpleNamespace(make_async_client=async_client_factory(200, content=b"four"))
    client.proxy = None
    client.timeout = 1
    client.default_headers = {}

    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_content_image(
            "https://example.test/large.jpg",
            referer="https://www.zhihu.com/question/1",
        )

    assert exc_info.value.code == "image_too_large"
    assert exc_info.value.retryable is False
    assert exc_info.value.http_status == 200


@pytest.mark.asyncio
async def test_transient_timeout_and_empty_response_recover_with_attempt_count(
    monkeypatch,
):
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(side_effect=[TimeoutError(), None, b"image-bytes"])
    logger = MagicMock()

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=logger,
        label="platform=test post_id=1 source_index=0",
    )

    assert payload == b"image-bytes"
    assert attempts == 3
    assert fetcher.await_count == 3
    assert logger.warning.call_count == 2
    logger.info.assert_called_once()


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_final_attempt_count(monkeypatch):
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(return_value=None)
    logger = MagicMock()

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=logger,
        label="platform=test post_id=2 source_index=0",
    )

    assert payload is None
    assert attempts == 3
    assert fetcher.await_count == 3
    assert logger.warning.call_count == 2
    logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_empty_bytes_are_retried_instead_of_treated_as_success(monkeypatch):
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(side_effect=[b"", b"", b"image-bytes"])

    payload, attempts = await image_download_retry.fetch_image_bytes_with_retry(
        fetcher,
        logger=MagicMock(),
        label="platform=test post_id=empty source_index=0",
    )

    assert payload == b"image-bytes"
    assert attempts == 3
    assert fetcher.await_count == 3


@pytest.mark.asyncio
async def test_terminal_http_error_is_not_retried(monkeypatch):
    fetcher = AsyncMock(
        side_effect=image_download_retry.classified_http_image_error(404, "HTTP 404")
    )

    with pytest.raises(image_download_retry.ImageDownloadFetchError) as exc_info:
        await image_download_retry.fetch_image_bytes_with_retry(
            fetcher,
            logger=MagicMock(),
            label="platform=test post_id=missing source_index=0",
        )

    assert exc_info.value.code == "image_source_unavailable"
    assert exc_info.value.http_status == 404
    assert exc_info.value.attempts == 1
    assert fetcher.await_count == 1


@pytest.mark.asyncio
async def test_retryable_http_error_preserves_status_after_exhaustion(monkeypatch):
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", (0.0, 0.0)
    )
    fetcher = AsyncMock(
        side_effect=image_download_retry.classified_http_image_error(503, "HTTP 503")
    )

    with pytest.raises(image_download_retry.ImageDownloadFetchError) as exc_info:
        await image_download_retry.fetch_image_bytes_with_retry(
            fetcher,
            logger=MagicMock(),
            label="platform=test post_id=unavailable source_index=0",
        )

    assert exc_info.value.code == "image_download_retryable"
    assert exc_info.value.http_status == 503
    assert exc_info.value.attempts == 3
    assert fetcher.await_count == 3


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "image_auth_required"), (403, "image_auth_required"), (429, "image_rate_limited")],
)
@pytest.mark.asyncio
async def test_run_level_http_error_is_classified_without_candidate_retry(
    status,
    code,
):
    fetcher = AsyncMock(
        side_effect=image_download_retry.classified_http_image_error(
            status, f"HTTP {status}"
        )
    )

    with pytest.raises(image_download_retry.ImageDownloadFetchError) as exc_info:
        await image_download_retry.fetch_image_bytes_with_retry(
            fetcher,
            logger=MagicMock(),
            label="platform=test post_id=blocked source_index=0",
        )

    assert exc_info.value.code == code
    assert exc_info.value.retryable is False
    assert exc_info.value.attempts == 1
    assert image_download_retry.is_runtime_blocking_image_error(code) is True
    assert fetcher.await_count == 1


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 6), color="blue").save(output, format="PNG")
    return output.getvalue()


def test_byte_limit_uses_formal_terminal_error_code(monkeypatch) -> None:
    monkeypatch.setattr("trippostcollect.artifacts.image_staging.MAX_IMAGE_BYTES", 3)

    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(b"four")

    assert exc_info.value.code == "image_too_large"


def test_non_image_response_uses_formal_terminal_error_code() -> None:
    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(b"<html>not an image</html>")

    assert exc_info.value.code == "image_non_raster_response"


def test_truncated_recognized_raster_uses_decode_error_code() -> None:
    content = png_bytes()

    with pytest.raises(image_manifest.ImageStagingError) as exc_info:
        image_manifest.inspect_image_bytes(content[: len(content) // 2])

    assert exc_info.value.code == "image_decode_failed"
