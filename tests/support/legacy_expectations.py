"""T14 固化的旧实现预期：根侧只运行根实现，与固化文件解码后的对象比较。

T14 删除 fork 子模块与私有桥 E 之前，各卡的双轨对照（旧实现与根实现在同一 fake 下运行并比较）
先把旧侧结果固化到 ``tests/fixtures/t14_legacy_expectations/<卡>/<组>/<用例>.json``。T14-C 删除 fork/E
时守卫与再生成功能随旧桥一起删除；固化文件不得再修改或用根实现重新生成，根实现有意改变行为时在根侧
比较中登记偏离（见 ``support.platform_session_deviation``）。

编码是带标签的 JSON，可还原原对照比较的对象：bytes（UTF-8 文本或 base64）、tuple、
非字符串键的 dict 都有标签，其他类型直接报错，避免静默改变比较粒度。唯一的折中：超过 16 KiB 的
bytes（stealth 脚本原文等）只固化 sha256 与长度，比较时对根侧实际字节求摘要。本模块不读取任何
TRIPPOSTCOLLECT_* 环境变量。
"""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "tests/fixtures/t14_legacy_expectations"
TAG = "$t14"

# 超过此长度的 bytes（如 stealth 脚本原文）只固化 sha256 与长度，比较时对实际字节求摘要。
DIGEST_BYTES_OVER = 16 * 1024


class BytesDigest:
    """固化的大块字节：与 bytes 比较时按 sha256 与长度判等，其余类型一律不等。"""

    def __init__(self, digest: str, size: int):
        self.digest, self.size = digest, size

    def __eq__(self, other):
        if isinstance(other, BytesDigest):
            return (self.digest, self.size) == (other.digest, other.size)
        if isinstance(other, bytes):
            return len(other) == self.size and sha256(other).hexdigest() == self.digest
        return NotImplemented

    def __hash__(self):
        return hash((self.digest, self.size))

    def __repr__(self):
        return f"BytesDigest(sha256={self.digest}, size={self.size})"


def encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, BytesDigest):
        return {TAG: "sha256", "v": value.digest, "size": value.size}
    if isinstance(value, bytes):
        if len(value) > DIGEST_BYTES_OVER:
            return {TAG: "sha256", "v": sha256(value).hexdigest(), "size": len(value)}
        try:
            return {TAG: "utf8", "v": value.decode("utf-8")}
        except UnicodeDecodeError:
            return {TAG: "b64", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {TAG: "tuple", "v": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        # dict 判等与插入顺序无关；按键排序，使目录遍历等顺序差异（如 macOS 与 Linux）不影响逐字节比较。
        if all(isinstance(key, str) for key in value) and TAG not in value:
            return {key: encode(value[key]) for key in sorted(value)}
        pairs = sorted(([encode(key), encode(item)] for key, item in value.items()),
                       key=lambda pair: json.dumps(pair[0], ensure_ascii=False, sort_keys=True))
        return {TAG: "dict", "v": pairs}
    raise TypeError(f"固化预期不支持的类型 {type(value).__name__}；请在两侧比较前显式归一")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(TAG)
    if kind is None:
        return {key: decode(item) for key, item in value.items()}
    if kind == "utf8":
        return value["v"].encode("utf-8")
    if kind == "b64":
        return base64.b64decode(value["v"])
    if kind == "sha256":
        return BytesDigest(value["v"], value["size"])
    if kind == "tuple":
        return tuple(decode(item) for item in value["v"])
    if kind == "dict":
        return {decode(key): decode(item) for key, item in value["v"]}
    raise ValueError(f"未知固化标签 {kind}")


def scrub(value: Any, *replacements: tuple[Any, str]) -> Any:
    """两侧同样把检出根与给定临时目录换成占位符，使固化结果不含主机路径。

    原双轨两侧本就在不同子目录运行且逐项相等，路径不会进入比较对象；这里只防止生成主机的
    检出位置被固化。只替换 str/bytes 中的子串（含 dict 键），不改变其他值。键名含 avatar 的值
    换成其 sha256 摘要，使固化文件不含头像 URL（两侧同样处理，仍逐值判等）。
    """
    pairs = sorted([(str(ROOT), "<ROOT>"), *((str(old), new) for old, new in replacements)],
                   key=lambda pair: len(pair[0]), reverse=True)

    def walk(item):
        if isinstance(item, str):
            for old, new in pairs:
                item = item.replace(old, new)
            return item
        if isinstance(item, bytes):
            for old, new in pairs:
                item = item.replace(old.encode(), new.encode())
            return item
        if isinstance(item, tuple):
            return tuple(walk(part) for part in item)
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {walk(key): _avatar_digest(part) if _is_avatar_key(key) else walk(part)
                    for key, part in item.items()}
        return item

    return walk(value)


def _is_avatar_key(key: Any) -> bool:
    return isinstance(key, str) and "avatar" in key.casefold()


def _avatar_digest(value: Any) -> str:
    """头像字段（平台响应在内存中可含头像）只固化其摘要：不落头像 URL，仍按值逐项判等。"""
    text = json.dumps(encode(value), ensure_ascii=False, sort_keys=True)
    return f"<avatar sha256={sha256(text.encode()).hexdigest()}>"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _lines(value: Any, depth: int) -> str:
    """前两层容器逐项换行（如 trace 每行一事件、files 每行一文件），更深层紧凑书写，便于审阅差异。"""
    if depth == 0 or not isinstance(value, (list, dict)) or not value:
        return _compact(value)
    pad = " " * (3 - depth)
    if isinstance(value, list):
        items = [pad + _lines(item, depth - 1) for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad[:-1] + "]"
    items = [pad + _compact(key) + ":" + _lines(item, depth - 1) for key, item in value.items()]
    return "{\n" + ",\n".join(items) + "\n" + pad[:-1] + "}"


def dumps(value: Any) -> str:
    return _lines(encode(value), 2) + "\n"


def relative(card: str, group: str, case: str) -> str:
    return f"{card}/{group}/{case}.json"


def load(card: str, group: str, case: str) -> Any:
    """根侧读取固化预期；文件必须在 manifest 登记且哈希一致。"""
    name = relative(card, group, case)
    data = (EXPECTATIONS / name).read_bytes()
    manifest = json.loads((EXPECTATIONS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][name]["sha256"] == sha256(data).hexdigest(), name
    return decode(json.loads(data))
