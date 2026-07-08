"""Image preview safety helpers."""

from __future__ import annotations

import ipaddress
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from trippostcollect.artifacts.paths import require_existing_project_file


class UnsafeImageUrl(ValueError):
    """Raised when a stored remote image URL is not safe to expose."""


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
    if _is_blocked_host(host):
        raise UnsafeImageUrl("image URL host is not allowed")
    return url


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
