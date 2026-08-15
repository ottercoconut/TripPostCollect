from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import types

import pytest

from trippostcollect.records.sanitization import (
    AUTHOR_AVATAR_LOG_REDACTION,
    redact_author_avatar_text,
    sanitize_author_avatar_data,
)


AVATAR_URL = "https://avatar.test/author.jpg"


def test_sanitizer_recursively_removes_aliases_and_exact_duplicate_urls() -> None:
    payload = {
        "avatar_url": AVATAR_URL,
        "author": {
            "name": "作者",
            "profile_url": "https://example.test/author",
            "nested": {"author_avatar": {"url": AVATAR_URL}},
            "copied_image": AVATAR_URL,
        },
        "items": [
            {"avatar": AVATAR_URL, "followers_count": 42},
            AVATAR_URL,
            "正文",
        ],
        "user_avatar": None,
    }

    result = sanitize_author_avatar_data(payload)

    assert result.value == {
        "author": {
            "name": "作者",
            "profile_url": "https://example.test/author",
            "nested": {},
        },
        "items": [{"followers_count": 42}, "正文"],
    }
    assert result.avatar_urls == frozenset({AVATAR_URL})
    assert result.removed_keys == 4
    assert result.removed_values == 2


@pytest.mark.parametrize(
    "alias",
    ["avatar_url", "author_avatar", "author_avatar_url", "avatar", "user_avatar"],
)
def test_all_known_avatar_aliases_are_removed(alias: str) -> None:
    result = sanitize_author_avatar_data({alias: AVATAR_URL, "author_id": "author-1"})

    assert result.value == {"author_id": "author-1"}
    assert result.avatar_urls == frozenset({AVATAR_URL})


def test_only_explicitly_evidenced_url_values_are_removed() -> None:
    body_url = "https://images.test/body.jpg"
    result = sanitize_author_avatar_data(
        {"image_url": body_url, "other": AVATAR_URL, "title": "青岛"},
        known_avatar_urls=[AVATAR_URL],
    )

    assert result.value == {"image_url": body_url, "title": "青岛"}


def test_non_url_avatar_value_does_not_remove_matching_research_text() -> None:
    result = sanitize_author_avatar_data(
        {"avatar": "默认头像", "description": "默认头像", "followers_count": 0}
    )

    assert result.value == {"description": "默认头像", "followers_count": 0}
    assert result.avatar_urls == frozenset()


def test_unstructured_child_output_with_avatar_key_is_discarded_whole() -> None:
    output = (
        'record={"avatar_url": "https://avatar.test/author.jpg"}\n'
        "later duplicate https://avatar.test/author.jpg\n"
    )

    sanitized, changed = redact_author_avatar_text(output)

    assert changed is True
    assert sanitized == AUTHOR_AVATAR_LOG_REDACTION
    assert AVATAR_URL not in sanitized


def test_unstructured_child_output_without_avatar_key_is_unchanged() -> None:
    output = "processed content image https://images.test/body.jpg\n"

    assert redact_author_avatar_text(output) == (output, False)


def test_mediacrawler_run_command_redacts_logs_and_summary_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from scripts import mediacrawler_crawl

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: int) -> tuple[bytes, bytes]:
            assert timeout == 10
            return (
                f'{{"avatar_url":"{AVATAR_URL}"}}\n'.encode(),
                f"duplicate={AVATAR_URL}\n".encode(),
            )

    monkeypatch.setattr(mediacrawler_crawl, "browser_launch_environment", lambda: {})
    monkeypatch.setattr(
        mediacrawler_crawl.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    result = mediacrawler_crawl.run_command(
        ["fake-child"],
        tmp_path,
        10,
        tmp_path / "logs",
    )

    assert (tmp_path / "logs/stdout.log").read_text(encoding="utf-8") == (
        AUTHOR_AVATAR_LOG_REDACTION
    )
    assert AVATAR_URL not in result["stdout_tail"]
    assert AVATAR_URL not in result["stderr_tail"]


def test_mediacrawler_exporter_sanitizes_before_jsonl_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.mediacrawler_export_entrypoint import sanitize_export_item

    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    serialized = json.dumps(
        sanitize_export_item(
            {
                "note_id": "note-1",
                "avatar_url": AVATAR_URL,
                "copied": AVATAR_URL,
                "title": "青岛",
            }
        ),
        ensure_ascii=False,
    )
    persisted = json.loads(serialized)
    assert persisted == {"note_id": "note-1", "title": "青岛"}


def test_mediacrawler_export_hook_wraps_writer_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import mediacrawler_export_entrypoint as entrypoint

    class FakeWriter:
        async def write_to_csv(self, item: dict, item_type: str) -> dict:
            return item

        async def write_to_jsonl(self, item: dict, item_type: str) -> dict:
            return item

        async def write_single_item_to_json(self, item: dict, item_type: str) -> dict:
            return item

    fake_tools = types.ModuleType("tools")
    fake_tools.__path__ = []  # type: ignore[attr-defined]
    fake_writer_module = types.ModuleType("tools.async_file_writer")
    fake_writer_module.AsyncFileWriter = FakeWriter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.async_file_writer", fake_writer_module)
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")

    entrypoint.install_export_hook()
    persisted = asyncio.run(
        FakeWriter().write_to_jsonl(
            {"avatar_url": AVATAR_URL, "copied": AVATAR_URL, "title": "青岛"},
            "contents",
        )
    )

    assert persisted == {"title": "青岛"}
