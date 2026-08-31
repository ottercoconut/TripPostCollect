"""Image preview safety helpers."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import mimetypes
from pathlib import Path
import socket
import ssl
from typing import Any, Collection
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trippostcollect.artifacts.paths import require_existing_project_file


class UnsafeImageUrl(ValueError):
    pass


class RemoteImageFetchError(ValueError):

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.retryable = retryable
        self.code = code


def remote_image_failure_code(error: RemoteImageFetchError) -> str:
    if error.code:
        return error.code
    if error.retryable:
        return "image_download_retryable"
    if error.http_status is not None:
        return "image_source_unavailable"
    return "image_non_raster_response"


@dataclass(frozen=True)
class RemoteImagePreview:
    content: bytes
    media_type: str
    final_url: str
    http_status: int = 200


REMOTE_IMAGE_REFERERS = {
    "bilibili": "https://www.bilibili.com/",
    "douyin": "https://www.douyin.com/",
    "weibo": "https://weibo.com/",
    "xhs": "https://www.xiaohongshu.com/",
    "zhihu": "https://www.zhihu.com/",
}

DEFAULT_REMOTE_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def content_type_for_path(path: Path, fallback: str = "application/octet-stream") -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or fallback


def local_image_file(local_path: str | None) -> Path | None:
    if not local_path:
        return None
    return require_existing_project_file(local_path)


def validate_remote_image_url(url: str | None) -> str:
    if not url:
        raise UnsafeImageUrl("empty image URL")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeImageUrl("image URL must use http or https")
    host = parsed.hostname
    if not host:
        raise UnsafeImageUrl("image URL host is missing")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeImageUrl("image URL credentials are not allowed")
    if _is_blocked_host(host):
        raise UnsafeImageUrl("image URL host is not allowed")
    return url


def remote_image_media_type(content_type: str | None, url: str) -> str:
    if content_type:
        media_type = content_type.split(";", 1)[0].strip().lower()
        return {"image/jpg": "image/jpeg"}.get(media_type, media_type)
    return content_type_for_path(Path(urlparse(url).path), fallback="application/octet-stream")


def validate_remote_image_response(
    *,
    content_type: str | None,
    content_length: str | int | None,
    url: str,
    max_bytes: int,
    allowed_media_types: Collection[str] | None = None,
) -> str:
    media_type = remote_image_media_type(content_type, url)
    if allowed_media_types is None:
        if not media_type.startswith("image/"):
            raise RemoteImageFetchError(f"remote image returned non-image content type: {media_type}")
    elif media_type not in allowed_media_types:
        raise RemoteImageFetchError(f"remote image returned unsupported content type: {media_type}")

    if content_length not in (None, ""):
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError) as exc:
            raise RemoteImageFetchError("remote image returned invalid Content-Length") from exc
        if declared_size < 0:
            raise RemoteImageFetchError("remote image returned negative Content-Length")
        if declared_size > max_bytes:
            raise RemoteImageFetchError(
                f"remote image exceeds {max_bytes} bytes", code="image_too_large"
            )
    return media_type


def read_limited_response(response: Any, *, max_bytes: int) -> bytes:
    content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise RemoteImageFetchError(
            f"remote image exceeds {max_bytes} bytes", code="image_too_large"
        )
    return content


def fetch_remote_image_preview(
    url: str | None,
    *,
    platform_key: str | None = None,
    max_bytes: int = DEFAULT_REMOTE_IMAGE_MAX_BYTES,
    timeout_seconds: int = 12,
) -> RemoteImagePreview:
    return fetch_remote_image_bytes(
        url,
        headers=_remote_image_headers(platform_key),
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )


def fetch_remote_image_bytes(
    url: str | None,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout_seconds: int,
    allowed_media_types: Collection[str] | None = None,
) -> RemoteImagePreview:
    current_url = validate_remote_image_url(url)
    opener = build_opener(_NoRedirectHandler)
    for _attempt in range(4):
        request = Request(current_url, headers=headers)
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                media_type = validate_remote_image_response(
                    content_type=response.headers.get("content-type"),
                    content_length=response.headers.get("content-length"),
                    url=current_url,
                    max_bytes=max_bytes,
                    allowed_media_types=allowed_media_types,
                )
                content = read_limited_response(response, max_bytes=max_bytes)
                if not content:
                    raise RemoteImageFetchError(
                        "remote image returned an empty response",
                        retryable=True,
                    )
                return RemoteImagePreview(
                    content=content,
                    media_type=media_type,
                    final_url=response.geturl(),
                    http_status=int(response.getcode() or 200),
                )
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                location = exc.headers.get("location")
                if not location:
                    raise RemoteImageFetchError(
                        f"remote image redirected without Location: HTTP {exc.code}",
                        http_status=exc.code,
                    ) from exc
                current_url = validate_remote_image_url(urljoin(current_url, location))
                continue
            if exc.code in {401, 403}:
                code = "image_auth_required"
            elif exc.code == 429:
                code = "image_rate_limited"
            else:
                code = None
            raise RemoteImageFetchError(
                f"remote image returned HTTP {exc.code}",
                http_status=exc.code,
                retryable=exc.code in {408, 425} or exc.code >= 500,
                code=code,
            ) from exc
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            raise RemoteImageFetchError(
                f"remote image fetch failed: {exc}",
                retryable=True,
            ) from exc
    raise RemoteImageFetchError("remote image redirected too many times")


def _is_blocked_host(host: str) -> bool:
    lowered = host.strip().lower().rstrip(".")
    if lowered in {"localhost"} or lowered.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return _is_private_or_local(address)


def _is_private_or_local(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def _remote_image_headers(platform_key: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    referer = REMOTE_IMAGE_REFERERS.get(platform_key or "")
    if referer:
        headers["Referer"] = referer
    return headers


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
