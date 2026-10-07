from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from trippostcollect.platforms.xhs.errors import XHSImageDownloadError
from trippostcollect.runtime.image_retry import ImageDownloadFetchError

from .support import XiaoHongShuCrawler, config


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (6, 4), color="green").save(output, format="PNG")
    return output.getvalue()


def image_note(note_id: str = "xhs-note") -> dict:
    return {
        "note_id": note_id,
        "image_list": [
            {
                "url_default": "https://sns-img-a.test/notes_pre_post/asset-one?format=jpg",
                "url": "https://sns-img-b.test/notes_pre_post/asset-one?format=webp",
                "url_pre": "https://sns-img-c.test/notes_pre_post/asset-one?format=avif",
            }
        ],
        "user": {
            "avatar": "https://sns-avatar.test/author.jpg",
            "user_id": "author",
        },
    }


@pytest.mark.asyncio
async def test_one_image_object_downloads_one_authoritative_url_with_true_format(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.xhs.media.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = png_bytes()

    await crawler.get_note_images(image_note())

    crawler.xhs_client.get_note_media.assert_awaited_once_with(
        "https://sns-img-a.test/notes_pre_post/asset-one?format=jpg"
    )
    assert (
        tmp_path / "xhs" / "images" / "xhs-note" / "000.png"
    ).read_bytes() == png_bytes()
    rows = [
        json.loads(line)
        for line in (tmp_path / "xhs" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["source_asset_key"] == "xhs:path:/notes_pre_post/asset-one"
    assert rows[0]["mime_type"] == "image/png"
    assert rows[0]["staging_path"] == "xhs/images/xhs-note/000.png"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.asyncio
async def test_transient_xhs_image_failure_recovers_and_records_attempts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.xhs.media.random.random", lambda: 0)
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.side_effect = [None, png_bytes()]

    await crawler.get_note_images(image_note("xhs-recovered"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "xhs" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.xhs_client.get_note_media.await_count == 2


@pytest.mark.asyncio
async def test_xhs_failed_image_writes_no_success_file_or_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.xhs.media.random.random", lambda: 0)
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = None

    with pytest.raises(XHSImageDownloadError):
        await crawler.get_note_images(image_note("xhs-failed"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "xhs" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.xhs_client.get_note_media.await_count == 3
    assert not (tmp_path / "xhs" / "images" / "xhs-failed").exists()


@pytest.mark.asyncio
async def test_terminal_http_fetch_error_preserves_manifest_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.xhs.media.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.side_effect = ImageDownloadFetchError(
        "HTTP 404",
        code="image_source_unavailable",
        retryable=False,
        http_status=404,
    )

    with pytest.raises(XHSImageDownloadError):
        await crawler.get_note_images(image_note("missing-note"))

    row = json.loads(
        (tmp_path / "xhs" / "image_manifest.jsonl").read_text().splitlines()[0]
    )
    assert row["error_code"] == "image_source_unavailable"
    assert row["http_status"] == 404
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_equivalent_xhs_asset_variants_are_deduplicated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.xhs.media.random.random", lambda: 0)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = AsyncMock()
    crawler.xhs_client.get_note_media.return_value = png_bytes()
    note = image_note("dedupe-note")
    note["image_list"].append(
        {"url_default": "http://sns-img-z.test/notes_pre_post/asset-one?format=png"}
    )

    await crawler.get_note_images(note)

    crawler.xhs_client.get_note_media.assert_awaited_once()
    files = list((tmp_path / "xhs" / "images" / "dedupe-note").iterdir())
    assert [path.name for path in files] == ["000.png"]
