"""#182 有意偏离：知乎关注数缺键不再补 0。

固化预期（``tests/fixtures/t14_legacy_expectations/T07``）不改、不用根实现重新生成。
T07 drive 的作者对象没有 ``following_count`` / ``followingCount``，旧实现把模型默认 0 写进 JSONL。
根侧比较先断言每条记录的 ``following_count`` 恰为整数 0，再换成 ``None``；
JSONL 字节里的 ``"following_count": 0,`` 同步换成 ``null``。没有该键的场景原样比较。
"""

from __future__ import annotations

from typing import Any


_NEEDLE = b'"following_count": 0,'
_REPLACEMENT = b'"following_count": null,'


def zhihu_records(expected: dict[str, Any]) -> dict[str, Any]:
    """把固化记录里因缺键补上的关注数 0 换成 null。真实键上的 0 不在这些场景里。"""
    records = []
    rewritten = 0
    for record in expected["records"]:
        if "following_count" not in record:
            records.append(record)
            continue
        assert type(record["following_count"]) is int and record["following_count"] == 0, record["following_count"]
        rewritten += 1
        records.append({**record, "following_count": None})
    files = {}
    file_hits = 0
    for name, blob in expected["files"].items():
        if not isinstance(blob, bytes) or b'"following_count"' not in blob:
            files[name] = blob
            continue
        assert blob.count(b'"following_count"') == blob.count(_NEEDLE), name
        file_hits += blob.count(_NEEDLE)
        files[name] = blob.replace(_NEEDLE, _REPLACEMENT)
    assert file_hits == rewritten, (file_hits, rewritten)
    if rewritten == 0:
        return expected
    return {**expected, "records": records, "files": files}
