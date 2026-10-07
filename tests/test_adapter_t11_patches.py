"""拒绝仍指向执行器重导出名字的无效 patch。"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTORS = {"mediacrawler_crawl", "scripts.mediacrawler_crawl"}


def executor_patches(source):
    """按导入和赋值来源跟踪模块别名，子属性 patch 不在此限。"""
    found = []

    def origin(node, bindings):
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = origin(node.value, bindings)
            return f"{parent}.{node.attr}" if parent else None
        if isinstance(node, ast.Call) and origin(node.func, bindings) in {
            "importlib.import_module", "__import__",
        } and node.args and isinstance(node.args[0], ast.Constant):
            return node.args[0].value
        return None

    def walk(node, bindings):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
            return
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local = dict(bindings)
            if not isinstance(node, ast.ClassDef):
                args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                args += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
                local.update({arg.arg: None for arg in args})
            for child in node.body:
                walk(child, local)
            bindings[node.name] = None
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setattr" and len(node.args) >= 2:
                target, name = node.args[:2]
                if origin(target, bindings) in EXECUTORS and isinstance(name, ast.Constant):
                    found.append((node.lineno, name.value))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and origin(target.value, bindings) in EXECUTORS:
                    found.append((target.lineno, target.attr))
            if node.value:
                walk(node.value, bindings)
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = origin(node.value, bindings)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, bindings)

    walk(ast.parse(source), {"__import__": "__import__"})
    return found


# T12 锁定验收以 `if hasattr(crawl, "MEDIACRAWLER_DIR")` 守卫该 patch：名字已随 fork 树检查删除，守卫使其成为
# 无操作，用于证明前置检查不再依赖 fork 目录。只豁免这一精确的（文件, 名字），其余死 patch 仍判失败。
GUARDED_REMOVED_NAMES = {("tests/test_adapter_t12.py", "MEDIACRAWLER_DIR")}


def test_executor_patches_have_script_readers():
    tree = ast.parse((ROOT / "scripts/mediacrawler_crawl.py").read_text())
    reads = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    failures = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for line, name in executor_patches(path.read_text()):
            if name not in reads and (relative, name) not in GUARDED_REMOVED_NAMES:
                failures.append(f"{path.relative_to(ROOT)}:{line}: {name}")
    assert not failures, "脚本没有读取被 patch 的名字：\n" + "\n".join(failures)


def test_executor_patch_alias_provenance():
    source = '''
import importlib as loader
from importlib import import_module as load
import mediacrawler_crawl as crawl
module = crawl
executor = load("mediacrawler_crawl")
def check(monkeypatch):
    local = loader.import_module("mediacrawler_crawl")
    monkeypatch.setattr(module, "first", 1)
    monkeypatch.setattr(executor, "second", 2)
    local.third = 3
    monkeypatch.setattr(crawl.time, "sleep", 4)
def unrelated(monkeypatch, module):
    monkeypatch.setattr(module, "ignored", 0)
crawl = load("different_module")
monkeypatch.setattr(crawl, "ignored", 0)
'''
    assert [name for _, name in executor_patches(source)] == ["first", "second", "third"]
