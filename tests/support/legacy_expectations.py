"""T14 固化的旧实现预期：根侧与之比较，守卫测试在旧实现仍存在时当场重算并逐字节比对。

T14 删除 fork 子模块与私有桥 E 之前，各卡的双轨对照（旧实现与根实现在同一 fake 下运行并比较）
先把旧侧结果固化到 ``tests/fixtures/t14_legacy_expectations/<卡>/<组>/<用例>.json``。之后：

- 根侧测试只运行根实现，与固化文件解码后的对象按原对照的 ``==`` 语义比较，不依赖 fork/E；
- 守卫测试（标记 ``t14_legacy_guard``）在 fork/E 存在时运行旧侧，把结果编码后与固化文件逐字节比较，
  证明固化文件就是旧实现的真实输出；T14-C 删除 fork/E 时随旧桥一起删除。

编码是带标签的 JSON，可还原原对照比较的对象：bytes（UTF-8 文本或 base64）、tuple、
非字符串键的 dict 都有标签，其他类型直接报错，避免静默改变比较粒度。唯一的折中：超过 16 KiB 的
bytes（stealth 脚本原文等）只固化 sha256 与长度，比较时对根侧实际字节求摘要。

再生成：``pytest --t14-write-legacy-expectations=DIR -m t14_legacy_guard <测试文件>``；守卫测试改为把
当场结果写到 DIR 并在会话结束时写 DIR/manifest.json（来源元数据与各文件 sha256），不再断言。
人工审阅差异后整体复制到固化目录。本模块不读取任何 TRIPPOSTCOLLECT_* 环境变量。
"""

from __future__ import annotations

import base64
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "tests/fixtures/t14_legacy_expectations"
FORK = ROOT / "tools/MediaCrawler"
BRIDGE = ROOT / "scripts/mediacrawler_export_entrypoint.py"
WRITE_OPTION = "--t14-write-legacy-expectations"
GUARD_MARKER = "t14_legacy_guard"
TAG = "$t14"
GENERATION_COMMAND = (
    "TPC sandbox: pytest -p pytest_asyncio.plugin -m t14_legacy_guard "
    "--t14-write-legacy-expectations=<DIR> <对应测试文件>"
)

# 本次会话由守卫写出的文件与其对应的原双轨测试（可多个旧路径共用一份预期），供 manifest 使用。
_written: dict[str, set[str]] = {}


def legacy_available() -> bool:
    """fork 入口与私有桥 E 均在时，旧侧才可当场运行。"""
    return (FORK / "main.py").is_file() and BRIDGE.is_file()


def legacy_guard(function):
    """守卫标记：只在 fork/E 存在时运行；T14-C 删除 fork/E 时同批删除这些用例。"""
    function = pytest.mark.skipif(not legacy_available(), reason="fork/E 已删除，守卫随旧桥退出")(function)
    return getattr(pytest.mark, GUARD_MARKER)(function)


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


LEGACY_ONLY_MARKER = "t14_legacy_only"


def legacy_only_marks():
    return (pytest.mark.skipif(not legacy_available(), reason="fork/E 已删除，旧桥自测随之退出"),
            getattr(pytest.mark, LEGACY_ONLY_MARKER))


def legacy_only(function):
    """只验证 fork/E 自身行为（盘点 b 类）的用例：fork/E 存在时运行，T14-C 随旧桥删除，无需迁移。

    参数化中只有旧桥一侧时用 ``pytest.param(..., marks=legacy_only_marks())``。
    """
    for mark in legacy_only_marks():
        function = mark(function)
    return function


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
        # dict 判等与插入顺序无关；按键排序，使目录遍历等顺序差异（如 macOS 与 Linux）不影响逐字节守卫。
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


def load_current(config, card: str, group: str, case: str) -> Any:
    """守卫读取比较基准：再生成模式下取本会话刚由冻结 fixture 路径写出的文件，否则取固化文件。"""
    target = config.getoption(WRITE_OPTION)
    if not target:
        return load(card, group, case)
    return decode(json.loads((Path(target) / relative(card, group, case)).read_text(encoding="utf-8")))


def check_legacy(config, card: str, group: str, case: str, value: Any, *, source_test: str) -> None:
    """守卫：旧侧当场结果编码后必须与固化文件逐字节一致；再生成模式下改为写出。"""
    text = dumps(value)
    target = config.getoption(WRITE_OPTION)
    name = relative(card, group, case)
    if target:
        path = Path(target) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in _written:
            # 多条旧路径共用一份预期时，后写者必须与先写者逐字节相同。
            assert path.read_text(encoding="utf-8") == text, f"旧路径结果不一致：{name}"
        path.write_text(text, encoding="utf-8")
        _written.setdefault(name, set()).add(source_test)
        return
    expected = (EXPECTATIONS / name).read_text(encoding="utf-8")
    assert text == expected, f"旧实现当场结果与固化预期 {name} 不一致"


def source_digest() -> dict[str, str]:
    """生成时旧实现的来源指纹：E 文件与 fork 内全部 .py（不含虚拟环境、浏览器数据与缓存）。"""
    digest = sha256()
    for path in sorted(FORK.rglob("*.py")):
        parts = path.relative_to(FORK).parts
        if any(part.startswith(".") or part in {"browser_data", "__pycache__"} for part in parts):
            continue
        digest.update(path.relative_to(FORK).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return {
        "bridge_sha256": sha256(BRIDGE.read_bytes()).hexdigest(),
        "fork_python_sources_sha256": digest.hexdigest(),
    }


def fork_commit() -> str | None:
    """读取子模块 HEAD；沙箱不可读 git 目录时返回 None，由 README 记录人工核对的提交。"""
    try:
        pointer = (FORK / ".git").read_text(encoding="utf-8").strip()
        gitdir = (FORK / pointer.removeprefix("gitdir: ")).resolve()
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head if not head.startswith("ref:") else None


def root_commit() -> str | None:
    """读取根检出的 HEAD 提交（worktree 经 commondir 解析引用）；不可读时返回 None。

    只记录 HEAD，不能反映生成时工作区中的未提交改动，manifest 以 root_commit_note 说明。
    """
    try:
        pointer = ROOT / ".git"
        gitdir = Path(pointer.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")) \
            if pointer.is_file() else pointer
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ")
        common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip() \
            if (gitdir / "commondir").is_file() else gitdir
        for base in (gitdir, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        return None
    return None


def write_manifest(target: str) -> None:
    """再生成会话结束时写出 manifest：来源元数据与各文件哈希。"""
    root = Path(target)
    previous = root / "manifest.json"
    known = json.loads(previous.read_text(encoding="utf-8"))["files"] if previous.is_file() else {}
    files = {}
    for path in sorted(root.rglob("*.json")):
        name = path.relative_to(root).as_posix()
        if name == "manifest.json":
            continue
        # 分批再生成时保留此前批次登记的原测试名。
        sources = sorted(_written[name]) if name in _written else known.get(name, {}).get("source_tests", [])
        files[name] = {"sha256": sha256(path.read_bytes()).hexdigest(), "source_tests": sources}
    manifest = {
        "generated_on": date.today().isoformat(),
        "generation_command": GENERATION_COMMAND,
        "fork_commit": fork_commit(),
        "root_commit": root_commit(),
        "root_commit_note": "生成时根检出的 HEAD；生成时工作区可能含未提交改动（如本批测试改动）。",
        **source_digest(),
        "files": files,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
    )
