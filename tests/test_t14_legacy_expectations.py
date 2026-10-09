"""T14 固化旧实现预期的完整性：登记、哈希、规范编码与不含主机路径/头像 URL。

固化目录的来源与再生成方式见 tests/fixtures/t14_legacy_expectations/README.md 与
tests/support/legacy_expectations.py。本文件不依赖 fork/E，T14-C 之后继续保留。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re

from support import legacy_expectations as expectations
from trippostcollect.records.sanitization import AUTHOR_AVATAR_KEYS, XHS_SERIALIZED_PROFILE_AVATAR_PATHS


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
    # 生成时根检出 HEAD；无法追溯时须为 null 并在说明中写明原因，不得伪造。
    assert MANIFEST["root_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", MANIFEST["root_commit"])
    assert MANIFEST["root_commit_note"].strip()
    assert re.fullmatch(r"[0-9a-f]{64}", MANIFEST["bridge_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", MANIFEST["fork_python_sources_sha256"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", MANIFEST["generated_on"])
    assert expectations.WRITE_OPTION in MANIFEST["generation_command"]


# 在项目头像键清单之外补充平台常见头像键；小红书作者图片键只在 user/author 等作者子对象内算头像。
EXTRA_AVATAR_KEYS = frozenset({"profile_image_url", "headimg"})
AUTHOR_OWNER_KEYS = frozenset({"user", "author", "user_info", "userinfo", "creator", "owner"})
AUTHOR_IMAGE_KEYS = frozenset({"image", "images", "imageb"})
URL = re.compile(r"(?:https?:)?//")


def _is_avatar_key(key, parent) -> bool:
    if not isinstance(key, str):
        return False
    name = key.casefold()
    return ("avatar" in name or name in AUTHOR_AVATAR_KEYS or name in EXTRA_AVATAR_KEYS
            or (name in AUTHOR_IMAGE_KEYS and parent in AUTHOR_OWNER_KEYS))


def _urls(value) -> list[str]:
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else value
        return [text] if URL.search(text) else []
    if isinstance(value, dict):
        return [url for item in value.values() for url in _urls(item)]
    if isinstance(value, (list, tuple)):
        return [url for item in value for url in _urls(item)]
    return []


def avatar_url_hits(value, parent=None) -> list[tuple[str, str]]:
    """按键名找出仍带 URL 的头像值；内嵌 JSON/JSONL 文本与小红书序列化作者资料一并展开检查。"""
    hits = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_avatar_key(key, parent):
                hits += [(key, url) for url in _urls(item)]
            if isinstance(key, str) and key.casefold() in XHS_SERIALIZED_PROFILE_AVATAR_PATHS and isinstance(item, str):
                try:
                    profile = json.loads(item)
                except ValueError:
                    profile = None
                for path in XHS_SERIALIZED_PROFILE_AVATAR_PATHS[key.casefold()]:
                    node = profile
                    for part in path:
                        node = node.get(part) if isinstance(node, dict) else None
                    hits += [("/".join(path), url) for url in _urls(node)]
            hits += avatar_url_hits(item, key.casefold() if isinstance(key, str) else None)
    elif isinstance(value, (list, tuple)):
        for item in value:
            hits += avatar_url_hits(item, parent)
    elif isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else value
        if text[:1] in "{[":
            for line in text.splitlines():
                try:
                    hits += avatar_url_hits(json.loads(line), parent)
                except ValueError:
                    continue
    return hits


def test_avatar_key_scan_catches_planted_values():
    planted = {
        "trace": [("http", {"body": '{"data":{"user":{"image":"https://cdn.test/u.jpg","nickname":"n"}}}'})],
        "files": {"a.jsonl": b'{"author": {"avatar_url": "//cdn.test/a.jpg"}}\n'},
        "record": {"profile_image_url": "https://cdn.test/p.jpg", "headimg": "https://cdn.test/h.jpg",
                   "creator_profile_json": '{"basicInfo": {"imageb": "https://cdn.test/b.jpg"}}'},
        "note": {"image": "https://cdn.test/body.jpg", "images": ["https://cdn.test/body2.jpg"]},
        "digest": {"avatar_thumb": "<avatar sha256=00>"},
    }
    assert sorted(key for key, _ in avatar_url_hits(planted)) == [
        "avatar_url", "basicInfo/imageb", "headimg", "image", "profile_image_url",
    ]


def test_files_are_canonical_and_free_of_host_paths_and_avatar_urls():
    root = str(expectations.ROOT)
    for name in expectation_files():
        text = (expectations.EXPECTATIONS / name).read_text(encoding="utf-8")
        # 文件即 dumps 的输出：解码再编码逐字节不变。
        assert expectations.dumps(expectations.decode(json.loads(text))) == text, name
        for marker in (root, "/home/", "/Users/", "/private/var/", "/tmp/"):
            assert marker not in text, (name, marker)
        assert not re.search(r"https?://[^\"\s]*avatar", text, re.IGNORECASE), name
        assert avatar_url_hits(expectations.decode(json.loads(text))) == [], name
