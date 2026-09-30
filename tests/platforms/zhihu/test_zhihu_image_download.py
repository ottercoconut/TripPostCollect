from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import AsyncMock, MagicMock

from PIL import Image
import pytest

from .support import config, make_crawler as ZhihuCrawler
from trippostcollect.application.contracts import ZhihuClientPorts
from trippostcollect.platforms.zhihu.client import ZhiHuClient
from trippostcollect.platforms.zhihu.models import ZhihuImageDownloadError
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.runtime.image_retry import ImageDownloadFetchError


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 5), color="purple").save(output, format="PNG")
    return output.getvalue()


def observed_content(content_id: str = "answer-1") -> ZhihuContent:
    return ZhihuContent(
        content_id=content_id,
        content_type="answer",
        content_url=f"https://www.zhihu.com/question/1/answer/{content_id}",
        content_detail_status="detail_observed",
        image_list=["https://pic1.zhimg.com/v2-body_r.jpg?source=token"],
        avatar_url="https://pic1.zhimg.com/v2-avatar.jpg",
        author_profile_url="https://www.zhihu.com/people/author",
    )


@pytest.mark.asyncio
async def test_client_image_request_reuses_session_headers_proxy_and_referer(monkeypatch):
    observed = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"image-"
            yield b"bytes"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, **kwargs):
            observed.update({"method": method, "url": url, **kwargs})
            return Response()

    def make_client(**kwargs):
        observed["proxy"] = kwargs.get("proxy")
        return Client()

    ports = ZhihuClientPorts(make_client, AsyncMock())
    client = ZhiHuClient(
        ports=ports,
        proxy="http://proxy.test:8080",
        headers={"cookie": "session=active", "user-agent": "test-agent"},
        playwright_page=MagicMock(),
        cookie_dict={"d_c0": "token"},
    )

    payload = await client.get_content_image(
        "https://pic1.zhimg.com/v2-body.jpg",
        referer="https://www.zhihu.com/question/1/answer/2",
    )

    assert payload == b"image-bytes"
    assert observed["proxy"] == "http://proxy.test:8080"
    assert observed["headers"]["cookie"] == "session=active"
    assert observed["headers"]["user-agent"] == "test-agent"
    assert observed["headers"]["referer"].endswith("/question/1/answer/2")


@pytest.mark.asyncio
async def test_observed_body_image_uses_referer_true_format_and_manifest(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_content_image.return_value = png_bytes()
    content = observed_content()

    await crawler.get_content_images(content)

    crawler.zhihu_client.get_content_image.assert_awaited_once_with(
        "https://pic1.zhimg.com/v2-body_r.jpg?source=token",
        referer=content.content_url,
    )
    stored = tmp_path / "zhihu" / "images" / "answer-1" / "000.png"
    assert stored.read_bytes() == png_bytes()
    rows = [
        json.loads(line)
        for line in (tmp_path / "zhihu" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["mime_type"] == "image/png"
    assert rows[0]["staging_path"] == "zhihu/images/answer-1/000.png"
    assert rows[0]["source_url"].endswith("?source=token")
    assert rows[0]["source_asset_key"].startswith("zhihu:urlsha256:")
    assert "cookie" not in json.dumps(rows).lower()


@pytest.mark.asyncio
async def test_formula_avatar_profile_and_zvideo_never_request_image_bytes(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    content = observed_content("excluded")
    content.image_list = [
        "https://www.zhihu.com/equation?tex=x",
        content.avatar_url,
        content.author_profile_url,
    ]
    video = ZhihuContent(
        content_id="video-1",
        content_type="zvideo",
        content_detail_status="detail_observed",
        image_list=["https://pic1.zhimg.com/video-cover.jpg"],
    )

    await crawler.get_content_images(content)
    await crawler.get_content_images(video)

    crawler.zhihu_client.get_content_image.assert_not_awaited()
    assert not (tmp_path / "zhihu").exists()


@pytest.mark.asyncio
async def test_transient_zhihu_image_failure_recovers_and_records_attempts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_content_image.side_effect = [None, png_bytes()]

    await crawler.get_content_images(observed_content("recovered-answer"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "zhihu" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.zhihu_client.get_content_image.await_count == 2


@pytest.mark.asyncio
async def test_failed_zhihu_image_has_only_failed_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_content_image.return_value = None

    with pytest.raises(ZhihuImageDownloadError):
        await crawler.get_content_images(observed_content("failed-answer"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "zhihu" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.zhihu_client.get_content_image.await_count == 3
    assert not (tmp_path / "zhihu" / "images" / "failed-answer").exists()


@pytest.mark.asyncio
async def test_terminal_http_fetch_error_preserves_manifest_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_content_image.side_effect = ImageDownloadFetchError(
        "HTTP 404",
        code="image_source_unavailable",
        retryable=False,
        http_status=404,
    )

    with pytest.raises(ZhihuImageDownloadError):
        await crawler.get_content_images(observed_content("missing-answer"))

    row = json.loads(
        (tmp_path / "zhihu" / "image_manifest.jsonl").read_text().splitlines()[0]
    )
    assert row["error_code"] == "image_source_unavailable"
    assert row["http_status"] == 404
    assert row["attempts"] == 1
