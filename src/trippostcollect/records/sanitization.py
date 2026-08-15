"""Remove author-avatar data before TripPostCollect persists structured records."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable
from urllib.parse import urlsplit


AUTHOR_AVATAR_KEYS = frozenset(
    {
        "avatar_url",
        "author_avatar",
        "author_avatar_url",
        "avatar",
        "user_avatar",
    }
)
AUTHOR_AVATAR_TEXT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:avatar_url|author_avatar|author_avatar_url|avatar|user_avatar)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
AUTHOR_AVATAR_LOG_REDACTION = "[TripPostCollect redacted child output containing author-avatar fields]\n"


@dataclass(frozen=True)
class AvatarSanitizationResult:
    value: Any
    avatar_urls: frozenset[str]
    removed_keys: int
    removed_values: int

    @property
    def changed(self) -> bool:
        return bool(self.removed_keys or self.removed_values)


def redact_author_avatar_text(value: str) -> tuple[str, bool]:
    """Discard unstructured child output when it exposes a known avatar field.

    Whole-output redaction prevents an evidenced avatar URL from surviving on
    another line and avoids guessing from hostnames, paths, or filenames.
    """

    if AUTHOR_AVATAR_TEXT_TOKEN_RE.search(value):
        return AUTHOR_AVATAR_LOG_REDACTION, True
    return value, False


def _is_avatar_key(value: Any) -> bool:
    return isinstance(value, str) and value.casefold() in AUTHOR_AVATAR_KEYS


def _evidenced_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    lowered = candidate.casefold()
    if len(candidate) > 8192 or not lowered.startswith(("http://", "https://", "//")):
        return None
    parsed = urlsplit(candidate if not candidate.startswith("//") else f"https:{candidate}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _collect_urls(value: Any, destination: set[str]) -> None:
    candidate = _evidenced_url(value)
    if candidate:
        destination.add(candidate)
        return
    if isinstance(value, dict):
        for nested in value.values():
            _collect_urls(nested, destination)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect_urls(nested, destination)


def discover_author_avatar_urls(value: Any) -> frozenset[str]:
    """Collect URLs found only beneath explicitly known avatar keys."""

    discovered: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, nested in node.items():
                if _is_avatar_key(key):
                    _collect_urls(nested, discovered)
                else:
                    visit(nested)
        elif isinstance(node, (list, tuple)):
            for nested in node:
                visit(nested)

    visit(value)
    return frozenset(discovered)


_REMOVED = object()


def sanitize_author_avatar_data(
    value: Any,
    *,
    known_avatar_urls: Iterable[str] = (),
) -> AvatarSanitizationResult:
    """Remove avatar keys and exact, evidenced duplicate avatar URL values.

    URLs are discovered exclusively from known avatar keys or supplied by the
    caller from explicit database avatar fields/relationships. No hostname,
    path, or filename heuristics are used.
    """

    avatar_urls = set(discover_author_avatar_urls(value))
    avatar_urls.update(
        candidate
        for raw_value in known_avatar_urls
        if (candidate := _evidenced_url(raw_value)) is not None
    )
    removed_keys = 0
    removed_values = 0

    def clean(node: Any) -> Any:
        nonlocal removed_keys, removed_values
        if isinstance(node, dict):
            cleaned: dict[Any, Any] = {}
            for key, nested in node.items():
                if _is_avatar_key(key):
                    removed_keys += 1
                    continue
                cleaned_value = clean(nested)
                if cleaned_value is _REMOVED:
                    continue
                cleaned[key] = cleaned_value
            return cleaned
        if isinstance(node, list):
            cleaned_list: list[Any] = []
            for nested in node:
                cleaned_value = clean(nested)
                if cleaned_value is not _REMOVED:
                    cleaned_list.append(cleaned_value)
            return cleaned_list
        if isinstance(node, tuple):
            cleaned_items: list[Any] = []
            for nested in node:
                cleaned_value = clean(nested)
                if cleaned_value is not _REMOVED:
                    cleaned_items.append(cleaned_value)
            return tuple(cleaned_items)
        if isinstance(node, str) and node.strip() in avatar_urls:
            removed_values += 1
            return _REMOVED
        return node

    cleaned = clean(value)
    if cleaned is _REMOVED:
        cleaned = None
    return AvatarSanitizationResult(
        value=cleaned,
        avatar_urls=frozenset(avatar_urls),
        removed_keys=removed_keys,
        removed_values=removed_values,
    )
