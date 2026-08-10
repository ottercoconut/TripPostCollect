from __future__ import annotations

from hashlib import sha256
import io
from pathlib import Path
from unittest import mock

from PIL import Image
import pytest

from trippostcollect.artifacts.image_candidates import ImageCandidate
from trippostcollect.artifacts.image_materialization import (
    DEFAULT_ARCHIVE_IMAGE_MAX_BYTES,
    DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS,
    ImageMaterializationError,
    promote_validated_image,
    safe_platform_post_id,
    validate_image_file,
    write_staging_image,
)
from trippostcollect.artifacts.image_proxy import (
    RemoteImageFetchError,
    UnsafeImageUrl,
    read_limited_response,
    validate_remote_image_response,
    validate_remote_image_url,
)


FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
    "AVIF": ("image/avif", ".avif"),
}


def test_default_archive_limits_match_governance() -> None:
    assert DEFAULT_ARCHIVE_IMAGE_MAX_BYTES == 20 * 1024 * 1024
    assert DEFAULT_ARCHIVE_IMAGE_MAX_PIXELS == 150_000_000


def image_bytes(image_format: str = "PNG", *, size: tuple[int, int] = (4, 3)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(output, format=image_format)
    return output.getvalue()


def candidate(*, post_id: str = "post-1") -> ImageCandidate:
    return ImageCandidate(
        platform_key="xhs",
        platform_post_id=post_id,
        image_role="content",
        source_index=0,
        source_url="https://sns-img.test/notes_pre_post/asset-a",
        source_key="image_list",
        source_asset_key="xhs:path:/notes_pre_post/asset-a",
    )


@pytest.mark.parametrize(("image_format", "expected"), FORMATS.items())
def test_allowed_raster_formats_are_staged_with_real_metadata(
    tmp_path: Path,
    image_format: str,
    expected: tuple[str, str],
) -> None:
    payload = image_bytes(image_format)
    mime_type, extension = expected

    validated = write_staging_image(
        [payload[:7], payload[7:]],
        staging_root=tmp_path,
        relative_stem="xhs/images/post-1/000",
        content_type=mime_type,
        content_length=str(len(payload)),
        source_url="https://example.test/image.bin",
    )

    assert validated.path.suffix == extension
    assert validated.mime_type == mime_type
    assert (validated.width, validated.height) == (4, 3)
    assert validated.sha256 == sha256(payload).hexdigest()
    assert validated.path.read_bytes() == payload
    assert list(tmp_path.rglob("*.part")) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"<html><body>not an image</body></html>",
        b'{"error":"not an image"}',
        b"\x00\x00\x00\x18ftypmp42video-bytes",
        b"RIFF\x10\x00\x00\x00WAVEaudio-bytes",
    ],
)
def test_non_raster_bytes_are_rejected(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)

    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(path, allowed_root=tmp_path)

    assert exc_info.value.code == "image_non_raster_response"


def test_forged_extension_truncation_hash_and_limits_are_rejected(tmp_path: Path) -> None:
    forged = tmp_path / "forged.jpg"
    forged.write_bytes(image_bytes("PNG"))
    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(forged, allowed_root=tmp_path)
    assert exc_info.value.code == "image_decode_failed"

    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(image_bytes("PNG")[:24])
    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(truncated, allowed_root=tmp_path)
    assert exc_info.value.code == "image_decode_failed"

    valid = tmp_path / "valid.png"
    valid.write_bytes(image_bytes("PNG", size=(10, 10)))
    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(valid, allowed_root=tmp_path, max_bytes=valid.stat().st_size - 1)
    assert exc_info.value.code == "image_too_large"
    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(valid, allowed_root=tmp_path, max_pixels=99)
    assert exc_info.value.code == "image_too_large"
    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(valid, allowed_root=tmp_path, expected_sha256="0" * 64)
    assert exc_info.value.code == "image_hash_mismatch"


def test_file_and_staging_paths_cannot_escape_controlled_roots(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(image_bytes())
    symlink = staging_root / "escape.png"
    symlink.symlink_to(outside)

    with pytest.raises(ImageMaterializationError) as exc_info:
        validate_image_file(symlink, allowed_root=staging_root)
    assert exc_info.value.code == "image_path_escape"

    with pytest.raises(ImageMaterializationError) as exc_info:
        write_staging_image(
            [image_bytes()],
            staging_root=staging_root,
            relative_stem="../escape",
        )
    assert exc_info.value.code == "image_path_escape"


def test_media_root_and_symlinked_platform_directory_cannot_escape_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    staging_root = project_root / "temp" / "staging"
    project_root.mkdir()
    staged = write_staging_image(
        [image_bytes()],
        staging_root=staging_root,
        relative_stem="xhs/images/post-1/000",
    )
    outside_media = tmp_path / "outside-media"
    with pytest.raises(ImageMaterializationError) as exc_info:
        promote_validated_image(
            staged,
            candidate(),
            staging_root=staging_root,
            media_root=outside_media,
            project_root=project_root,
        )
    assert exc_info.value.code == "image_path_escape"
    assert not outside_media.exists()

    outside_media.mkdir()
    media_root = project_root / "data" / "media"
    media_root.mkdir(parents=True)
    (media_root / "xhs").symlink_to(outside_media)
    with pytest.raises(ImageMaterializationError) as exc_info:
        promote_validated_image(
            staged,
            candidate(),
            staging_root=staging_root,
            media_root=media_root,
            project_root=project_root,
        )
    assert exc_info.value.code == "image_path_escape"
    assert list(outside_media.iterdir()) == []


def test_content_hash_promotion_is_idempotent_and_uses_safe_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    staging_root = project_root / "temp" / "staging"
    media_root = project_root / "data" / "media"
    project_root.mkdir()
    staged = write_staging_image(
        [image_bytes("WEBP")],
        staging_root=staging_root,
        relative_stem="xhs/images/post/000",
    )
    item = candidate(post_id="帖子/../../../unsafe")

    first = promote_validated_image(
        staged,
        item,
        staging_root=staging_root,
        media_root=media_root,
        project_root=project_root,
    )
    second = promote_validated_image(
        staged,
        item,
        staging_root=staging_root,
        media_root=media_root,
        project_root=project_root,
    )

    assert first.reused is False
    assert second.reused is True
    assert first.local_path == second.local_path
    assert first.local_path.startswith(f"data/media/xhs/{safe_platform_post_id(item.platform_post_id)}/000-")
    assert first.local_path.endswith(".webp")
    assert (project_root / first.local_path).is_file()
    assert len(list(media_root.rglob("*.webp"))) == 1
    assert list(media_root.rglob("*.part")) == []


def test_existing_long_term_hash_path_with_different_bytes_is_a_conflict(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    staging_root = project_root / "temp" / "staging"
    media_root = project_root / "data" / "media"
    project_root.mkdir()
    staged = write_staging_image(
        [image_bytes("PNG")],
        staging_root=staging_root,
        relative_stem="xhs/images/post-1/000",
    )
    item = candidate()
    first = promote_validated_image(
        staged,
        item,
        staging_root=staging_root,
        media_root=media_root,
        project_root=project_root,
    )
    (project_root / first.local_path).write_bytes(image_bytes("JPEG"))

    with pytest.raises(ImageMaterializationError) as exc_info:
        promote_validated_image(
            staged,
            item,
            staging_root=staging_root,
            media_root=media_root,
            project_root=project_root,
        )
    assert exc_info.value.code == "image_promotion_conflict"


def test_promotion_failure_after_replace_removes_new_target(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    staging_root = project_root / "temp" / "staging"
    media_root = project_root / "data" / "media"
    project_root.mkdir()
    staged = write_staging_image(
        [image_bytes("PNG")],
        staging_root=staging_root,
        relative_stem="xhs/images/post-rollback/000",
    )

    with mock.patch(
        "trippostcollect.artifacts.image_materialization._fsync_directory",
        side_effect=OSError("simulated fsync failure"),
    ):
        with pytest.raises(OSError, match="simulated fsync failure"):
            promote_validated_image(
                staged,
                candidate(post_id="post-rollback"),
                staging_root=staging_root,
                media_root=media_root,
                project_root=project_root,
            )

    assert not list(media_root.rglob("*.*"))


def test_interrupted_staging_replace_leaves_only_identifiable_part(tmp_path: Path) -> None:
    with mock.patch(
        "trippostcollect.artifacts.image_materialization.os.replace",
        side_effect=OSError("simulated interruption"),
    ):
        with pytest.raises(OSError, match="simulated interruption"):
            write_staging_image(
                [image_bytes("PNG")],
                staging_root=tmp_path,
                relative_stem="xhs/images/post-1/000",
            )

    assert list(tmp_path.rglob("000.png")) == []
    parts = list(tmp_path.rglob("*.part"))
    assert len(parts) == 1
    assert validate_image_file(parts[0], allowed_root=tmp_path).mime_type == "image/png"


def test_response_headers_and_stream_limits_fail_before_success(tmp_path: Path) -> None:
    payload = image_bytes("PNG")
    with pytest.raises(ImageMaterializationError) as exc_info:
        write_staging_image(
            [payload],
            staging_root=tmp_path,
            relative_stem="too-large",
            content_type="image/png",
            content_length=len(payload) + 1,
            max_bytes=len(payload),
        )
    assert exc_info.value.code == "image_too_large"

    with pytest.raises(ImageMaterializationError) as exc_info:
        write_staging_image(
            [payload],
            staging_root=tmp_path,
            relative_stem="octet-stream",
            content_type="application/octet-stream",
        )
    assert exc_info.value.code == "image_non_raster_response"


def test_shared_preview_response_validation_keeps_preview_semantics() -> None:
    assert validate_remote_image_response(
        content_type="image/svg+xml; charset=utf-8",
        content_length="12",
        url="https://example.test/image.svg",
        max_bytes=20,
    ) == "image/svg+xml"
    with pytest.raises(RemoteImageFetchError, match="unsupported content type"):
        validate_remote_image_response(
            content_type="image/svg+xml",
            content_length="12",
            url="https://example.test/image.svg",
            max_bytes=20,
            allowed_media_types={"image/jpeg", "image/png"},
        )
    with pytest.raises(RemoteImageFetchError, match="exceeds"):
        read_limited_response(io.BytesIO(b"12345"), max_bytes=4)


def test_nonstandard_jpeg_content_type_is_normalized() -> None:
    assert validate_remote_image_response(
        content_type="image/jpg",
        content_length="12",
        url="https://example.test/image.jpg",
        max_bytes=20,
        allowed_media_types={"image/jpeg", "image/png"},
    ) == "image/jpeg"


def test_remote_url_validation_rejects_local_and_credentialed_urls() -> None:
    for url in (
        "http://127.0.0.1/image.png",
        "http://[::1]/image.png",
        "file:///tmp/image.png",
        "https://user:pass@example.test/image.png",
    ):
        with pytest.raises(UnsafeImageUrl):
            validate_remote_image_url(url)
