"""T14-C 有意偏离：冻结旧源码引用的两个导入位置随 fork 与过渡模块删除。

T08/T10/T11 的冻结旧源码（tests/fixtures/adapter_t08、adapter_t10、adapter_t11 下逐字保存的 scripts 原文）
在执行时导入了两个 T14-C 删除的名字：

1. ``trippostcollect.core.paths.MEDIACRAWLER_DIR``（fork 目录常量，原值 ``PROJECT_ROOT / "tools" / "MediaCrawler"``）；
2. 过渡模块 ``execution_state``（原 ``scripts/execution_state.py``，只把自身转发到
   ``trippostcollect.core.execution_state``）。

固化文件不改、哈希断言照旧针对原字节。执行前对源码做“钉住旧值 → 单向替换”：先断言旧导入恰好出现
调用方声明的预期次数，再把 ``MEDIACRAWLER_DIR`` 从 ``core.paths`` 导入中移除并以原值预置到模块命名空间（测试照旧可
monkeypatch 为临时目录），把 ``from execution_state import`` 换成转发目标的同名导入。其余源码逐字执行。
"""

from __future__ import annotations

import ast
from typing import Any

from trippostcollect.core import paths


MEDIACRAWLER_DIR_NAME = "MEDIACRAWLER_DIR"
LEGACY_EXECUTION_STATE_IMPORT = "from execution_state import "
ROOT_EXECUTION_STATE_IMPORT = "from trippostcollect.core.execution_state import "


def legacy_mediacrawler_dir():
    """被删除常量的原定义：TOOLS_ROOT / "MediaCrawler"，TOOLS_ROOT = PROJECT_ROOT / "tools"。"""
    return paths.PROJECT_ROOT / "tools" / "MediaCrawler"


def frozen_source(source: str, *, mediacrawler_dir_imports: int, execution_state_imports: int) -> tuple[str, dict[str, Any]]:
    """返回可执行的冻结源码与需预置的命名空间。

    调用方按冻结文件给出两类旧导入的预期次数（0 或 1）；实际出现次数必须恰好相等，替换后旧导入必须
    全部消失，防止 fixture 或替换规则变化时静默跳过偏离。
    """
    namespace: dict[str, Any] = {}
    imports = [node for node in ast.parse(source).body
               if isinstance(node, ast.ImportFrom) and node.module == "trippostcollect.core.paths"
               and any(alias.name == MEDIACRAWLER_DIR_NAME for alias in node.names)]
    assert len(imports) == mediacrawler_dir_imports, (len(imports), mediacrawler_dir_imports)
    if imports:
        lines = source.splitlines(keepends=True)
        start, end = imports[0].lineno - 1, imports[0].end_lineno
        statement = "".join(lines[start:end])
        multiline, inline = f"    {MEDIACRAWLER_DIR_NAME},\n", f" {MEDIACRAWLER_DIR_NAME}, "
        assert statement.count(multiline) + statement.count(inline) == 1, statement
        replaced = statement.replace(multiline, "").replace(inline, " ")
        source = "".join(lines[:start]) + replaced + "".join(lines[end:])
        namespace[MEDIACRAWLER_DIR_NAME] = legacy_mediacrawler_dir()
    assert not any(
        isinstance(node, ast.ImportFrom) and any(alias.name == MEDIACRAWLER_DIR_NAME for alias in node.names)
        for node in ast.parse(source).body
    ), "MEDIACRAWLER_DIR 旧导入未被替换"
    marker = f"\n{LEGACY_EXECUTION_STATE_IMPORT}"
    assert source.count(LEGACY_EXECUTION_STATE_IMPORT) == source.count(marker) == execution_state_imports, (
        source.count(LEGACY_EXECUTION_STATE_IMPORT), execution_state_imports)
    source = source.replace(marker, f"\n{ROOT_EXECUTION_STATE_IMPORT}")
    assert LEGACY_EXECUTION_STATE_IMPORT not in source, "execution_state 旧导入未被替换"
    return source, namespace
