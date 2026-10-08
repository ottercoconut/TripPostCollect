"""T14 固化旧实现预期的完整性：登记、哈希、规范编码与不含主机路径/头像 URL。

固化目录的来源与再生成方式见 tests/fixtures/t14_legacy_expectations/README.md 与
tests/support/legacy_expectations.py。本文件不依赖 fork/E，T14-C 之后继续保留。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re

from support import legacy_expectations as expectations


MANIFEST = json.loads((expectations.EXPECTATIONS / "manifest.json").read_text(encoding="utf-8"))


def expectation_files():
    return sorted(
        path.relative_to(expectations.EXPECTATIONS).as_posix()
        for path in expectations.EXPECTATIONS.rglob("*.json") if path.name != "manifest.json"
    )


def test_manifest_registers_every_file_with_hash_and_source_test():
    assert sorted(MANIFEST["files"]) == expectation_files()
    for name, entry in MANIFEST["files"].items():
        assert entry["sha256"] == sha256((expectations.EXPECTATIONS / name).read_bytes()).hexdigest(), name
        assert entry["source_tests"] and all("::" in test for test in entry["source_tests"]), name


def test_manifest_records_legacy_provenance():
    assert re.fullmatch(r"[0-9a-f]{40}", MANIFEST["fork_commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", MANIFEST["bridge_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", MANIFEST["fork_python_sources_sha256"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", MANIFEST["generated_on"])
    assert expectations.WRITE_OPTION in MANIFEST["generation_command"]


def test_files_are_canonical_and_free_of_host_paths_and_avatar_urls():
    root = str(expectations.ROOT)
    for name in expectation_files():
        text = (expectations.EXPECTATIONS / name).read_text(encoding="utf-8")
        # 文件即 dumps 的输出：解码再编码逐字节不变。
        assert expectations.dumps(expectations.decode(json.loads(text))) == text, name
        for marker in (root, "/home/", "/Users/", "/private/var/", "/tmp/"):
            assert marker not in text, (name, marker)
        assert not re.search(r"https?://[^\"\s]*avatar", text, re.IGNORECASE), name
