from __future__ import annotations

from io import BytesIO
import asyncio
import json
from dataclasses import replace

from PIL import Image
import httpx
import pytest

from support.weibo_adapter import settings, crawler as make_crawler, client_ports
from trippostcollect.platforms.weibo.client import WeiboClient, is_sinaimg_url
from trippostcollect.platforms.weibo.core import WeiboImageDownloadError
from trippostcollect.platforms.weibo.search_page import WeiboPageCapture
from trippostcollect.runtime.image_retry import ImageDownloadFetchError

config = settings()


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color="blue").save(output, format="PNG")
    return output.getvalue()


def test_only_sinaimg_hosts_are_requested() -> None:
    assert is_sinaimg_url("https://wx4.sinaimg.cn/large/example.jpg")
    assert is_sinaimg_url("http://wx1.sinaimg.cn/mw2000/a.jpg")
    assert not is_sinaimg_url("https://wx.test/example.jpg")
    assert not is_sinaimg_url("https://i1.wp.com/wx4.sinaimg.cn/large/example.jpg")


class _StreamResponse:
    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self._content = content
        self.request = httpx.Request("GET", "https://wx1.sinaimg.cn/x.jpg")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("status", request=self.request, response=response)

    async def aiter_bytes(self):
        yield self._content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _RecordingClient:
    def __init__(self, responses: list[tuple[int, bytes]]):
        self.responses = list(responses)
        self.seen: list[tuple[str, dict, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        self.seen.append((url, kwargs["headers"], kwargs.get("proxy", "absent")))
        status, content = self.responses.pop(0)
        return _StreamResponse(status, content)


def _client(responses, *, max_bytes: int | None = None) -> tuple[WeiboClient, _RecordingClient]:
    http = _RecordingClient(responses)
    ports = client_ports(make_async_client=lambda **kwargs: http)
    if max_bytes is not None:
        ports = replace(ports, image_max_bytes=max_bytes)
    return WeiboClient(user_agent="ua-test", ports=ports, timeout=5), http


@pytest.mark.asyncio
async def test_image_request_sends_only_ua_referer_and_accept() -> None:
    client, http = _client([(200, png_bytes())])
    body = await client.get_note_image(
        [
            "https://wx1.sinaimg.cn/large/example.jpg",
            "https://evil.test/example.jpg",
        ]
    )
    assert body == png_bytes()
    assert http.seen == [(
        "https://wx1.sinaimg.cn/large/example.jpg",
        {
            "user-agent": "ua-test",
            "accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
            "referer": "https://weibo.com/",
        },
        "absent",
    )]


@pytest.mark.asyncio
async def test_image_falls_through_specs_on_failure_and_oversize() -> None:
    client, http = _client(
        [(404, b""), (200, b"x" * 20), (200, b"ok-bytes")],
        max_bytes=8,
    )
    body = await client.get_note_image(
        [
            "https://wx1.sinaimg.cn/large/a.jpg",
            "https://wx1.sinaimg.cn/mw2000/a.jpg",
            "https://wx1.sinaimg.cn/orj1080/a.jpg",
        ]
    )
    assert body == b"ok-bytes"
    assert [item[0] for item in http.seen] == [
        "https://wx1.sinaimg.cn/large/a.jpg",
        "https://wx1.sinaimg.cn/mw2000/a.jpg",
        "https://wx1.sinaimg.cn/orj1080/a.jpg",
    ]


@pytest.mark.asyncio
async def test_non_sinaimg_url_is_not_requested() -> None:
    client, http = _client([(200, png_bytes())])
    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image("https://wx.test/example.jpg")
    assert exc_info.value.code == "image_source_unavailable"
    assert http.seen == []


@pytest.mark.asyncio
async def test_every_oversized_spec_is_terminal() -> None:
    client, http = _client([(200, b"x" * 20), (200, b"y" * 20)], max_bytes=8)
    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image(
            ["https://wx1.sinaimg.cn/large/a.jpg", "https://wx1.sinaimg.cn/orj1080/a.jpg"]
        )
    assert exc_info.value.code == "image_too_large"
    assert len(http.seen) == 2


@pytest.mark.asyncio
async def test_image_403_is_auth_required() -> None:
    client, http = _client([(403, b"")])
    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image("https://wx1.sinaimg.cn/large/a.jpg")
    assert exc_info.value.code == "image_auth_required"
    assert exc_info.value.http_status == 403
    assert len(http.seen) == 1


@pytest.mark.asyncio
async def test_image_auth_and_rate_limit_stop_before_the_next_spec() -> None:
    client, http = _client([(401, b""), (200, png_bytes())])
    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image(
            ["https://wx1.sinaimg.cn/large/a.jpg", "https://wx1.sinaimg.cn/orj1080/a.jpg"]
        )
    assert exc_info.value.code == "image_auth_required"
    assert exc_info.value.http_status == 401
    assert len(http.seen) == 1

    client, http = _client([(429, b"")])
    with pytest.raises(ImageDownloadFetchError) as exc_info:
        await client.get_note_image("https://wx4.sinaimg.cn/large/a.jpg")
    assert exc_info.value.code == "image_rate_limited"


def _record() -> dict:
    return {
        "note_id": "note-download",
        "image_assets": [
            {
                "pid": "pid-first",
                "url": "https://wx1.sinaimg.cn/large/one.jpg",
                "urls": ["https://wx1.sinaimg.cn/large/one.jpg"],
                "source_index": 0,
            },
            {
                "pid": "pid-second",
                "url": "https://wx2.sinaimg.cn/large/two.jpg",
                "urls": ["https://wx2.sinaimg.cn/large/two.jpg"],
                "source_index": 1,
            },
        ],
        "content_visit_refs": {"ledger_id": "L", "images": []},
    }


@pytest.mark.asyncio
async def test_note_images_do_not_sleep_between_cdn_direct_visits(monkeypatch, tmp_path):
    """同一帖多张正文图之间不再固定睡眠，每张仍按 source_index 登记 cdn_direct。"""

    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.jsonl"))
    crawler = make_crawler(settings(
        ENABLE_GET_MEIDAS=True,
        SAVE_DATA_PATH=str(tmp_path),
        CRAWLER_MAX_SLEEP_SEC=2,
    ))
    slept: list[float] = []

    async def record_sleep(seconds: float = 0) -> None:
        slept.append(float(seconds))

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    class Client:
        async def get_note_image(self, urls):
            del urls
            return png_bytes()

    crawler.wb_client = Client()
    crawler._capture = WeiboPageCapture.open(object())
    record = _record()

    await crawler.get_note_images(record)

    assert slept == []
    ledger = tmp_path / "behavior.content_visits.jsonl"
    visits = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "visit"
    ]
    assert [visit["source"] for visit in visits] == ["cdn_direct", "cdn_direct"]
    assert [visit["stage"] for visit in visits] == ["note_image", "note_image"]
    assert [visit["asset_key"] for visit in visits] == [
        "weibo:pid:pid-first",
        "weibo:pid:pid-second",
    ]
    assert record["content_visit_refs"]["images"] == [visit["id"] for visit in visits]


@pytest.mark.asyncio
async def test_get_note_images_passes_note_pid_order_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = make_crawler(config)

    class Client:
        def __init__(self):
            self.calls = []

        async def get_note_image(self, urls):
            self.calls.append(list(urls))
            return png_bytes()

    crawler.wb_client = Client()
    await crawler.get_note_images(_record())

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(row["platform_post_id"], row["source_index"]) for row in rows] == [
        ("note-download", 0),
        ("note-download", 1),
    ]
    assert [row["source_asset_key"] for row in rows] == [
        "weibo:pid:pid-first",
        "weibo:pid:pid-second",
    ]
    assert crawler.wb_client.calls[0] == ["https://wx1.sinaimg.cn/large/one.jpg"]


@pytest.mark.asyncio
async def test_transient_fetch_recovers_and_records_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = make_crawler(config)

    class Client:
        def __init__(self):
            self.calls = 0

        async def get_note_image(self, urls):
            self.calls += 1
            if self.calls == 1:
                return None
            return png_bytes()

    crawler.wb_client = Client()
    record = _record()
    record["image_assets"] = record["image_assets"][:1]
    await crawler.get_note_images(record)
    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [("downloaded", 2)]
    assert crawler.wb_client.calls == 2


@pytest.mark.asyncio
async def test_failed_fetch_writes_only_failed_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = make_crawler(config)

    class Client:
        def __init__(self):
            self.calls = 0

        async def get_note_image(self, urls):
            self.calls += 1
            return None

    crawler.wb_client = Client()
    record = _record()
    record["image_assets"] = record["image_assets"][:1]
    with pytest.raises(WeiboImageDownloadError):
        await crawler.get_note_images(record)
    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert crawler.wb_client.calls == 3


@pytest.mark.asyncio
async def test_terminal_http_fetch_error_preserves_manifest_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = make_crawler(config)

    class Client:
        async def get_note_image(self, urls):
            raise ImageDownloadFetchError(
                "HTTP 404", code="image_source_unavailable", retryable=False, http_status=404,
            )

    crawler.wb_client = Client()
    record = _record()
    record["image_assets"] = record["image_assets"][:1]
    with pytest.raises(WeiboImageDownloadError):
        await crawler.get_note_images(record)
    row = json.loads((tmp_path / "weibo" / "image_manifest.jsonl").read_text().splitlines()[0])
    assert row["error_code"] == "image_source_unavailable"
    assert row["http_status"] == 404
    assert row["attempts"] == 1
