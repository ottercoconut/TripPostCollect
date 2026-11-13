"""#185 有意偏离：知乎每条记录点击作者页，并纳入访问门禁。

固化文件不改。比较前在详情页偏离之后再做单向替换：

1. 预期轨迹去掉为旧 httpx 客户端读取 Cookie 的 ``cookies`` 事件。
2. 成功的回答/文章把粉丝来源改成作者页，并补上关注数、作品数和四个原值。
3. 停止事件里的 ``source_visit_id`` 随账本序号变化，比较前从实际事件去掉。
"""

from __future__ import annotations

import json
from typing import Any


AUTHOR_COUNTS = {
    "answerCount": 2,
    "articlesCount": 1,
    "pinsCount": 0,
    "questionCount": 3,
}

PARSER_AND_MODEL_REPLACEMENTS = (
    (
        "class DataFetchError(RequestError):",
        "class DataFetchError(Exception):",
    ),
    (
        '    """something error when fetch"""',
        '    """A search or detail read that did not produce a usable platform payload."""',
    ),
    (
        '            res.url_token = author.get("url_token") or ""',
        '            res.url_token = author.get("url_token") or author.get("urlToken") or ""',
    ),
    (
        '        description="Detail visit reference prepared for the visit gate",\n'
        '    )',
        '        description="Search, detail, author and image visit references for the visit gate",\n'
        '    )\n'
        '    author_posts_count: int | None = Field(\n'
        '        default=None,\n'
        '        description="answerCount + articlesCount from the author page; None when either count is missing",\n'
        '    )\n'
        '    author_page_counts: dict = Field(\n'
        '        default_factory=dict,\n'
        '        description="Raw author-page counts stored in author_json; missing keys are omitted",\n'
        '    )',
    ),
    (
        '    if detail_content.content_visit_refs:\n'
        '        search_content.content_visit_refs = dict(detail_content.content_visit_refs)\n'
        '    search_content.content_detail_status = "detail_observed"',
        '    if detail_content.content_visit_refs:\n'
        '        search_content.content_visit_refs = dict(detail_content.content_visit_refs)\n'
        '    # 只有作者页补全过的详情才覆盖搜索作者；未进作者页时保留搜索侧粉丝证据。\n'
        '    if detail_content.author_followers_source in {"author_page", "missing"}:\n'
        '        search_content.followers_count = detail_content.followers_count\n'
        '        search_content.followers_observed = detail_content.followers_observed\n'
        '        search_content.author_followers_source = detail_content.author_followers_source\n'
        '        search_content.following_count = detail_content.following_count\n'
        '        search_content.author_posts_count = detail_content.author_posts_count\n'
        '        search_content.author_page_counts = dict(detail_content.author_page_counts)\n'
        '        if detail_content.creator_url_token:\n'
        '            search_content.creator_url_token = detail_content.creator_url_token\n'
        '    search_content.content_detail_status = "detail_observed"',
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
    if not str(record.get("content_text") or "").strip():
        return record
    record["author_followers_source"] = "author_page"
    record["followers_observed"] = True
    record["followers_count"] = 42
    record["following_count"] = 6
    record["author_posts_count"] = 3
    record["author_page_counts"] = dict(AUTHOR_COUNTS)
    return record


def _rewrite_jsonl(blob: bytes, *, from_expected: bool) -> bytes:
    lines = []
    for raw in blob.splitlines():
        if not raw.strip():
            continue
        item = _project_record(json.loads(raw), from_expected=from_expected)
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _strip_source_visit(events: list) -> list:
    rewritten = []
    for event in events:
        event = json.loads(json.dumps(event))
        details = event.get("details")
        if isinstance(details, dict):
            details.pop("source_visit_id", None)
        rewritten.append(event)
    return rewritten


def align(actual: dict[str, Any], expected: dict[str, Any], scenario: str) -> tuple[dict, dict]:
    """抹平作者页字段和来源访问序号。``scenario`` 保留给调用方，本变换不按场景分叉。"""

    del scenario
    expected = {
        **expected,
        "trace": [row for row in expected["trace"] if row[0] != "cookies"],
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
        "events": _strip_source_visit(actual["events"]),
    }
    return actual, expected
