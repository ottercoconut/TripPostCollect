"""T14 守护：fork 离线 lane 的 34 个用例逐项映射到根测试断言（`tests/fixtures/t14_fork_test_mapping.json`）。

fork 仍在时按 AST 展开 fork 用例（含 parametrize 自动 id），要求映射恰好覆盖；映射引用的根测试函数必须真实
存在。fork 删除后映射成为留档：根测试仍须存在，且不得再有依赖 fork 的根测试（T14-C 须先移植）。
card_gate 的 passed 数下降不能作为覆盖已迁移的证据，缺口以 status=gap/partial 显式列出。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORK = ROOT / "tools" / "MediaCrawler"
MAPPING = json.loads((ROOT / "tests/fixtures/t14_fork_test_mapping.json").read_text(encoding="utf-8"))
STATUSES = {"equivalent", "partial", "gap"}


def _test_functions(path: Path) -> dict[str, ast.AST]:
    """顶层测试函数与 `Class::method`，与 pytest node 的函数段一致。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[f"{node.name}::{member.name}"] = member
    return found


def _parametrize_ids(node: ast.AST) -> list[str] | None:
    """只支持字面量参数表：pytest 自动 id 为各值以 '-' 连接。"""
    for decorator in node.decorator_list:
        if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "parametrize"):
            values = ast.literal_eval(decorator.args[1])
            return ["-".join(map(str, value if isinstance(value, tuple) else (value,))) for value in values]
    return None


def test_mapping_lists_every_fork_node_once_with_valid_status() -> None:
    nodes = [item["fork_node"] for item in MAPPING["nodes"]]
    assert len(nodes) == len(set(nodes)) == MAPPING["fork_expected_tests"] == 34
    for item in MAPPING["nodes"]:
        assert item["fork_node"].split("::")[0] in MAPPING["fork_files"], item["fork_node"]
        assert item["status"] in STATUSES and item["subject"], item["fork_node"]
        if item["status"] == "equivalent":
            assert item["root"] and "gap" not in item, item["fork_node"]
        else:
            # 部分覆盖与缺口必须写明缺什么，以及移植时的目标测试文件。
            assert item["gap"] and item["planned_target"], item["fork_node"]
        if item["status"] == "partial":
            assert item["root"], item["fork_node"]


def test_mapped_root_tests_exist() -> None:
    cache: dict[str, dict] = {}
    for item in MAPPING["nodes"]:
        for reference in item["root"]:
            path, _, function = reference["test"].partition("::")
            assert reference["asserts"], reference["test"]
            if path not in cache:
                assert (ROOT / path).is_file(), reference["test"]
                cache[path] = _test_functions(ROOT / path)
            assert function in cache[path], reference["test"]


def test_mapping_matches_fork_offline_nodes_while_fork_exists() -> None:
    if not FORK.is_dir():
        # fork 已删除：依赖 fork 的根测试必须已移植并清除标记，映射只作留档。
        assert [reference["test"] for item in MAPPING["nodes"] for reference in item["root"]
                if reference.get("fork_dependent")] == []
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("t14_run_matrix", ROOT / "scripts/ci/run_matrix.py")
    run_matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_matrix)
    assert tuple(MAPPING["fork_files"]) == run_matrix.FORK_OFFLINE_TESTS
    assert MAPPING["fork_expected_tests"] == run_matrix.FORK_EXPECTED_TESTS
    expected = set()
    for relative in MAPPING["fork_files"]:
        for name, node in _test_functions(FORK / relative).items():
            if not name.split("::")[-1].startswith("test_"):
                continue
            ids = _parametrize_ids(node)
            expected.update(f"{relative}::{name}" + (f"[{case}]" if ids is not None else "")
                            for case in (ids if ids is not None else [None]))
    assert {item["fork_node"] for item in MAPPING["nodes"]} == expected
