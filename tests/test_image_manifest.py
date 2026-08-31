"""TripPostCollect tests for image manifest."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from trippostcollect.artifacts.image_candidates import (
    ImageCandidate,
    content_image_candidates,
    source_asset_key_for_image,
)
from trippostcollect.artifacts.image_manifest import (
    ImageManifestEntry,
    ImageManifestError,
    manifest_sha256,
    parse_manifest,
    serialize_manifest,
    validate_post_manifest,
)


def candidate(index: int = 0) -> ImageCandidate:
    return ImageCandidate(
        platform_key="douyin",
        platform_post_id="post-1",
        image_role="content",
        source_index=index,
        source_url=f"https://p3-sign.test/images/asset-{index}.jpeg?signature=one",
        source_key="note_download_url",
        source_asset_key=f"douyin:uri:asset-{index}",
    )


def downloaded_entry(item: ImageCandidate | None = None) -> ImageManifestEntry:
    item = item or candidate()
    return ImageManifestEntry(
        schema_version=1,
        platform_key=item.platform_key,
        platform_post_id=item.platform_post_id,
        image_role=item.image_role,
        source_index=item.source_index,
        source_key=item.source_key,
        source_asset_key=item.source_asset_key,
        source_url=item.source_url,
        fetch_status="downloaded",
        attempts=1,
        http_status=200,
        staging_path=f"douyin/images/{item.platform_post_id}/{item.source_index:03d}.bin",
        size_bytes=1024,
        mime_type="image/jpeg",
        width=1080,
        height=1440,
        sha256="a" * 64,
        error_code=None,
    )


def failed_entry(item: ImageCandidate | None = None) -> ImageManifestEntry:
    item = item or candidate()
    return ImageManifestEntry(
        schema_version=1,
        platform_key=item.platform_key,
        platform_post_id=item.platform_post_id,
        image_role=item.image_role,
        source_index=item.source_index,
        source_key=item.source_key,
        source_asset_key=item.source_asset_key,
        source_url=item.source_url,
        fetch_status="failed",
        attempts=3,
        http_status=503,
        staging_path=None,
        size_bytes=None,
        mime_type=None,
        width=None,
        height=None,
        sha256=None,
        error_code="image_download_retryable",
    )


def test_platform_stable_keys_ignore_transport_variants() -> None:
    assert source_asset_key_for_image(
        "bilibili",
        "http://i0.hdslb.com/bfs/article/abcdef.jpg@1048w_!web-dynamic.webp?x=1",
    ) == source_asset_key_for_image(
        "bilibili",
        "https://i2.hdslb.com/bfs/article/abcdef.png?x=2",
    )
    assert source_asset_key_for_image(
        "weibo",
        "https://wx1.sinaimg.cn/large/old.jpg?token=one",
        asset_metadata={"pid": "pid-123"},
    ) == source_asset_key_for_image(
        "weibo",
        "http://wx4.sinaimg.cn/orj360/new.webp?token=two",
        asset_metadata={"pid": "pid-123"},
    )
    assert source_asset_key_for_image(
        "xhs",
        "https://sns-img-a.test/notes_pre_post/asset-a?format=jpg",
    ) == source_asset_key_for_image(
        "xhs",
        "http://sns-img-b.test/notes_pre_post/asset-a?format=webp",
    )
    assert source_asset_key_for_image(
        "douyin",
        "https://p3-sign-a.test/image/path-a?signature=one",
        asset_metadata={"uri": "uri-123"},
    ) == source_asset_key_for_image(
        "douyin",
        "https://p3-sign-b.test/image/path-b?signature=two",
        asset_metadata={"uri": "uri-123"},
    )
    assert source_asset_key_for_image(
        "zhihu",
        "https://pic1.zhimg.com/v2-asset_r.jpg?source=one",
    ) == source_asset_key_for_image(
        "zhihu",
        "http://pic4.zhimg.com/v2-asset_b.webp?source=two",
    )


def test_fallback_keys_are_deterministic_and_distinct() -> None:
    first = source_asset_key_for_image(
        "douyin",
        "https://host-a.test/images/asset-a.jpeg?signature=one",
    )
    changed_signature = source_asset_key_for_image(
        "douyin",
        "http://host-b.test/images/asset-a.jpeg?signature=two",
    )
    different_asset = source_asset_key_for_image(
        "douyin",
        "https://host-a.test/images/asset-b.jpeg?signature=one",
    )

    assert first == changed_signature
    assert first.startswith("douyin:urlsha256:")
    assert first != different_asset


def test_candidates_use_weibo_pid_and_douyin_uri_metadata() -> None:
    weibo = content_image_candidates(
        "weibo",
        {
            "note_id": "wb-1",
            "image_list": ["https://wx.test/a.jpg", "https://wx.test/b.jpg"],
            "image_assets": [
                {"pid": "pid-a", "url": "https://wx.test/a.jpg"},
                {"pid": "pid-b", "url": "https://wx.test/b.jpg"},
            ],
        },
    )
    douyin = content_image_candidates(
        "douyin",
        {
            "aweme_id": "dy-1",
            "note_download_url": "https://dy.test/a.jpg,https://dy.test/b.jpg",
            "image_assets": [
                {"uri": "uri-a", "url": "https://dy.test/a.jpg"},
                {"uri": "uri-b", "url": "https://dy.test/b.jpg"},
            ],
        },
    )

    assert [item.source_asset_key for item in weibo] == [
        "weibo:pid:pid-a",
        "weibo:pid:pid-b",
    ]
    assert [item.source_asset_key for item in douyin] == [
        "douyin:uri:uri-a",
        "douyin:uri:uri-b",
    ]


def test_mediacrawler_weibo_manifest_sample_matches_root_candidates() -> None:
    record = {
        "note_id": "wb-note-1",
        "image_list_source": "mblog.pics",
        "image_list": ["https://wx1.sinaimg.cn/large/body.jpg?token=one"],
        "image_assets": [
            {
                "pid": "pid-body-1",
                "url": "https://wx1.sinaimg.cn/large/body.jpg?token=one",
                "source_index": 0,
            }
        ],
    }
    payload = json.dumps(
        {
            "schema_version": 1,
            "platform_key": "weibo",
            "platform_post_id": "wb-note-1",
            "image_role": "content",
            "source_index": 0,
            "source_key": "image_list",
            "source_asset_key": "weibo:pid:pid-body-1",
            "source_url": "https://wx1.sinaimg.cn/large/body.jpg?token=one",
            "fetch_status": "downloaded",
            "attempts": 1,
            "http_status": 200,
            "staging_path": "weibo/images/wb-note-1/000.png",
            "size_bytes": 96,
            "mime_type": "image/png",
            "width": 4,
            "height": 3,
            "sha256": "b" * 64,
            "error_code": None,
        },
        sort_keys=True,
    )
    entries = parse_manifest(payload + "\n")
    candidates = content_image_candidates("weibo", record)

    assert validate_post_manifest(entries, candidates) == entries
    assert entries[0].staging_path == "weibo/images/wb-note-1/000.png"


def test_manifest_round_trip_sorting_and_hash_are_deterministic() -> None:
    second_candidate = candidate(1)
    entries = [downloaded_entry(second_candidate), downloaded_entry()]

    payload = serialize_manifest(entries)
    parsed = parse_manifest(payload)

    assert [entry.source_index for entry in parsed] == [0, 1]
    assert serialize_manifest(parsed) == payload
    assert manifest_sha256(entries) == manifest_sha256(reversed(entries))
    assert b"cookie" not in payload.lower()
    assert b"storage_state" not in payload.lower()
    assert b"headers" not in payload.lower()


@pytest.mark.parametrize(
    "staging_path",
    ["/tmp/image.jpg", "../image.jpg", "douyin/../../image.jpg", r"douyin\image.jpg", "C:/image.jpg"],
)
def test_manifest_rejects_path_escape(staging_path: str) -> None:
    with pytest.raises(ImageManifestError) as exc_info:
        replace(downloaded_entry(), staging_path=staging_path)
    assert exc_info.value.code == "image_path_escape"


def test_manifest_rejects_unknown_schema_missing_fields_and_sensitive_extras() -> None:
    payload = downloaded_entry().to_dict()
    with pytest.raises(ImageManifestError):
        ImageManifestEntry.from_dict({**payload, "schema_version": 2})

    missing = dict(payload)
    missing.pop("sha256")
    with pytest.raises(ImageManifestError, match="missing=.*sha256"):
        ImageManifestEntry.from_dict(missing)

    with pytest.raises(ImageManifestError, match="extra=.*cookie"):
        ImageManifestEntry.from_dict({**payload, "cookie": "secret"})


def test_manifest_rejects_incomplete_success_and_forged_failure() -> None:
    with pytest.raises(ImageManifestError) as exc_info:
        replace(downloaded_entry(), sha256=None)
    assert exc_info.value.code == "image_hash_mismatch"

    payload = failed_entry().to_dict()
    payload["staging_path"] = "douyin/images/post-1/000.bin"
    with pytest.raises(ImageManifestError, match="success metadata"):
        ImageManifestEntry.from_dict(payload)


def test_manifest_rejects_duplicate_identity() -> None:
    line = json.dumps(downloaded_entry().to_dict(), sort_keys=True)
    with pytest.raises(ImageManifestError, match="duplicate manifest identity") as exc_info:
        parse_manifest(f"{line}\n{line}\n")
    assert exc_info.value.code == "image_manifest_identity_mismatch"


def test_post_manifest_requires_exact_count_and_identity() -> None:
    item = candidate()
    entry = downloaded_entry(item)

    assert validate_post_manifest([entry], [item]) == (entry,)

    with pytest.raises(ImageManifestError) as exc_info:
        validate_post_manifest([], [item])
    assert exc_info.value.code == "image_manifest_count_mismatch"

    with pytest.raises(ImageManifestError) as exc_info:
        validate_post_manifest([replace(entry, source_asset_key="douyin:uri:other")], [item])
    assert exc_info.value.code == "image_manifest_identity_mismatch"


def test_failed_manifest_row_is_auditable_but_not_complete() -> None:
    item = candidate()
    entry = failed_entry(item)

    assert validate_post_manifest([entry], [item], require_downloaded=False) == (entry,)
    with pytest.raises(ImageManifestError) as exc_info:
        validate_post_manifest([entry], [item])
    assert exc_info.value.code == "image_download_retryable"
