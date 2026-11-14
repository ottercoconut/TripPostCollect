"""#193 有意偏离：知乎认证只认作者页官方身份。

固化文件不改。比较前在作者页偏离之后再做单向替换：

1. 搜索作者不再把 ``verify_bayes`` / ``badge_v2.title`` 写入 ``verified_text``。
2. ``ZhihuContent`` 增加 ``author_verified`` 与 ``author_page_badges``。
3. 作者页补全时把认证结果覆盖到搜索记录。
4. 固化预期里的假作者页没有 ``badgeV2``，因此 ``author_verified`` 为空、
   ``verified_text`` 为空、``author_page_badges`` 为空对象。
"""

from __future__ import annotations

import json
from typing import Any


PARSER_AND_MODEL_REPLACEMENTS = (
    (
        '            res.headline = author.get("headline") or ""\n'
        '            badge_v2 = author.get("badge_v2") or {}\n'
        '            res.verified_text = author.get("verify_bayes") or badge_v2.get("title") or ""\n',
        '            res.headline = author.get("headline") or ""\n',
    ),
    (
        '    author_page_counts: dict = Field(\n'
        '        default_factory=dict,\n'
        '        description="Raw author-page counts stored in author_json; missing keys are omitted",\n'
        '    )',
        '    author_page_counts: dict = Field(\n'
        '        default_factory=dict,\n'
        '        description="Raw author-page counts stored in author_json; missing keys are omitted",\n'
        '    )\n'
        '    author_verified: int | None = Field(\n'
        '        default=None,\n'
        '        description="Author-page official identity: 1 passed, 0 read but absent, None unknown",\n'
        '    )\n'
        '    author_page_badges: dict = Field(\n'
        '        default_factory=dict,\n'
        '        description="Author-page badges for raw_sample_json; icon, url, avatar and image keys removed",\n'
        '    )',
    ),
    (
        '        search_content.author_posts_count = detail_content.author_posts_count\n'
        '        search_content.author_page_counts = dict(detail_content.author_page_counts)\n'
        '        if detail_content.creator_url_token:\n',
        '        search_content.author_posts_count = detail_content.author_posts_count\n'
        '        search_content.author_page_counts = dict(detail_content.author_page_counts)\n'
        '        search_content.author_verified = detail_content.author_verified\n'
        '        search_content.verified_text = detail_content.verified_text\n'
        '        search_content.author_page_badges = dict(detail_content.author_page_badges)\n'
        '        if detail_content.creator_url_token:\n',
    ),
)


def parser_and_model_source(text: str) -> str:
    for old, new in PARSER_AND_MODEL_REPLACEMENTS:
        if old not in text:
            continue
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def _project_record(record: dict, *, from_expected: bool) -> dict:
    record = dict(record)
    if not from_expected or record.get("content_type") not in {"answer", "article"}:
        return record
    record["author_verified"] = None
    record["verified_text"] = ""
    record["author_page_badges"] = {}
    return record


def _rewrite_jsonl(blob: bytes, *, from_expected: bool) -> bytes:
    lines = []
    for raw in blob.splitlines():
        if not raw.strip():
            continue
        item = _project_record(json.loads(raw), from_expected=from_expected)
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def align(actual: dict[str, Any], expected: dict[str, Any], scenario: str) -> tuple[dict, dict]:
    """假作者页没有官方身份结构，固化预期按未判定对齐。``scenario`` 保留给调用方。"""

    del scenario
    expected = {
        **expected,
        "records": [_project_record(record, from_expected=True) for record in expected["records"]],
        "files": {
            path: _rewrite_jsonl(blob, from_expected=True) if "/jsonl/" in str(path) and str(path).endswith(".jsonl") else blob
            for path, blob in expected["files"].items()
        },
    }
    actual = {
        **actual,
        "records": [_project_record(record, from_expected=False) for record in actual["records"]],
        "files": {
            path: _rewrite_jsonl(blob, from_expected=False) if "/jsonl/" in str(path) and str(path).endswith(".jsonl") else blob
            for path, blob in actual["files"].items()
        },
    }
    return actual, expected
