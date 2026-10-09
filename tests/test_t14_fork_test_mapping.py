"""T14 守护：fork 离线 lane 的 34 个用例逐项映射到根测试断言（`tests/fixtures/t14_fork_test_mapping.json`）。

T14-B2 起 34 个用例已按台账 target_file 原名移植（ported_to），T14-C 随 fork 删除 fork 离线 lane。

T14-C 删除 fork 后映射成为留档：映射引用的根测试函数仍须存在，34 项须全部移植为 equivalent，
不得再有依赖 fork 的根测试或 gap 条目。
card_gate 的 passed 数下降不能作为覆盖已迁移的证据，缺口以 status=gap/partial 显式列出。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


def gap_problems(mapping: dict) -> list[str]:
    """partial/gap 必须写明 missing；fork 已删除，不允许任何 gap 留存。"""
    problems = [f"{item['fork_node']}: partial/gap 缺少 missing 说明" for item in mapping["nodes"]
                if item["status"] != "equivalent" and not str(item.get("missing", "")).strip()]
    problems += [f"{item['fork_node']}: fork 已删除但仍为 gap" for item in mapping["nodes"]
                 if item["status"] == "gap"]
    return problems


def test_mapping_has_no_gaps() -> None:
    assert gap_problems(MAPPING) == []


def test_gap_gate_rejects_leftover_gap_and_empty_missing() -> None:
    nodes = [{"fork_node": "a", "status": "gap", "missing": "x"},
             {"fork_node": "b", "status": "partial", "missing": " "},
             {"fork_node": "c", "status": "equivalent"}]
    assert gap_problems({"nodes": nodes}) == ["b: partial/gap 缺少 missing 说明", "a: fork 已删除但仍为 gap"]


def test_mapping_lists_every_fork_node_once_with_valid_status() -> None:
    nodes = [item["fork_node"] for item in MAPPING["nodes"]]
    assert len(nodes) == len(set(nodes)) == MAPPING["fork_expected_tests"] == 34
    for item in MAPPING["nodes"]:
        assert item["fork_node"].split("::")[0] in MAPPING["fork_files"], item["fork_node"]
        assert item["status"] in STATUSES and item["subject"], item["fork_node"]
        if item["status"] == "equivalent":
            assert item["root"] and "missing" not in item, item["fork_node"]
        else:
            # 部分覆盖与缺口必须写明缺什么，以及移植时的目标测试文件。
            assert item["missing"].strip() and item["planned_target"], item["fork_node"]
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


def test_mapping_is_complete_archive_after_fork_removal() -> None:
    """T14-C 删除 fork 后映射只作留档：34 项全部按原名移植且为 equivalent，无 fork 依赖。"""
    assert [reference["test"] for item in MAPPING["nodes"] for reference in item["root"]
            if reference.get("fork_dependent")] == []
    assert len(MAPPING["nodes"]) == 34
    assert all(item.get("ported_to") and item["status"] == "equivalent" for item in MAPPING["nodes"])


def test_ported_nodes_exist_under_same_name_and_parameters() -> None:
    """ported_to 指向同名根 node：函数在台账目标文件中，参数 id 与原 fork 用例一致，并列为首个根引用。"""
    for item in MAPPING["nodes"]:
        if not item.get("ported_to"):
            continue
        assert item["status"] == "equivalent", item["fork_node"]
        fork_name = item["fork_node"].split("::", 1)[1]
        path, _, name = item["ported_to"].partition("::")
        assert name == fork_name, item["fork_node"]
        function, _, case = name.partition("[")
        node = _test_functions(ROOT / path)[function]
        ids = _parametrize_ids(node)
        assert (case.rstrip("]") in ids) if case else ids is None, item["ported_to"]
        assert item["root"][0]["test"] == f"{path}::{function}", item["fork_node"]
