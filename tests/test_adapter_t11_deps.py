"""T11 依赖方向、唯一常量与独立首导入的离线防线。"""

from __future__ import annotations

import ast
from dataclasses import fields
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/trippostcollect"
ALLOWED = {
    "application/collection.py": {
        "artifacts.formal_images", "artifacts.paths", "artifacts.image_candidates",
        "artifacts.image_materialization", "db.bootstrap", "db.connection", "db.content",
        "db.discovery_read", "platforms.registry", "records.formal", "records.sanitization",
        "records.topic_relevance", "runtime.behavior", "runtime.browser_launcher",
        "runtime.browser_runtime", "runtime.cookies", "runtime.helpers", "runtime.process",
        "scheduler.discovery", "xhs.batch_checkpoint",
    },
    "application/reporting.py": {"records.formal", "runtime.behavior", "runtime.helpers"},
    "application/failures.py": {"records.text_signals"},
    "artifacts/formal_images.py": {
        "application.contracts", "records.formal", "runtime.helpers", "runtime.process",
    },
    "artifacts/paths.py": {"application.contracts"},
    "db/content.py": {
        "application.contracts", "artifacts.image_candidates", "artifacts.image_persistence",
        "artifacts.image_materialization", "records.formal", "runtime.helpers",
    },
}
CONSTANTS = {
    "db.content": ("FORMAL_SQLITE_BUSY_TIMEOUT_MS",),
    "application.reporting": ("SAMPLE_KEYS", "AUTHOR_FIELD_MARKERS", "IMAGE_SUFFIXES", "VIDEO_SUFFIXES"),
    "application.collection": (
        "SKIPPED_CANDIDATE_EVENT_FIELDS", "DISCOVERY_RESEED_EVENT_FIELDS",
        "XHS_NETWORK_RECOVERY_WAIT_SECONDS", "XHS_NETWORK_RETRY_MIN_SECONDS",
        "XHS_NETWORK_RETRY_MAX_SECONDS", "XHS_OPERATOR_LOGIN_WAIT_SECONDS",
    ),
    "application.failures": ("RUNTIME_BLOCKING_FAILURE_TYPES",),
    "artifacts.formal_images": ("RASTER_IMAGE_SUFFIX_RE", "LEGACY_ZHIHU_TRANSFORM_SUFFIX_RE"),
}


def imports(tree):
    """包括函数内与注解块；从包导入模块时还原完整模块路径。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "目标模块不得通过相对导入绕过依赖清单"
            for alias in node.names:
                module = node.module or ""
                if module in {"trippostcollect.records", "trippostcollect.core"}:
                    module += "." + alias.name
                yield node, module


@pytest.mark.parametrize("relative", sorted(ALLOWED))
def test_closed_dependency_modules(relative):
    tree = ast.parse((PACKAGE / relative).read_text())
    assert any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names) for n in tree.body)
    layer = relative.split("/")[0]
    annotations = {id(child) for n in ast.walk(tree) if isinstance(n, ast.If)
                   and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING"
                   for body in n.body for child in ast.walk(body)}
    actual = set()
    for node, module in imports(tree):
        assert module.split(".")[0] not in {
            "base", "cache", "config", "constant", "database", "libs",
            "media_platform", "model", "proxy", "store", "tools", "var", "MediaCrawler",
        }, (relative, module)
        if module.startswith("scripts") or module == "mediacrawler_crawl":
            assert relative == "application/collection.py"
            assert module == "scripts.mediacrawler_crawl" and id(node) in annotations
            assert [a.name for a in node.names] == ["XhsSupervisorRuntimeReporter"]
        if module.startswith("trippostcollect."):
            short = module.removeprefix("trippostcollect.")
            actual.add(short)
            if short == "xhs.batch_checkpoint":
                # 原函数体内的常量导入沿用原位定义，适用常量导入例外。
                assert {a.name for a in node.names} == {"DATA_ROOT_ENV", "ENABLED_ENV", "RESUME_ENV"}
            assert short.startswith((layer + ".", "core.")) or short in ALLOWED[relative], short
            if layer in {"artifacts", "db"} and short.startswith("application."):
                assert short == "application.contracts"
            if relative == "db/content.py":
                assert short != "artifacts.formal_images"
            if relative == "artifacts/formal_images.py":
                assert not short.startswith("db.")
        if isinstance(node, ast.ImportFrom):
            annotation_names = {"MaterializedImage", "ImageCandidate", "ImageManifestEntry", "ValidatedImage",
                                "XhsSupervisorRuntimeReporter"}
            if any(alias.name in annotation_names for alias in node.names):
                assert id(node) in annotations
    assert actual, "未读取到项目依赖，不能以空集冒充检查通过"


def test_package_has_no_script_or_fork_imports():
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        type_nodes = {id(child) for n in ast.walk(tree) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING"
                      for body in n.body for child in ast.walk(body)}
        for node in ast.walk(tree):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                [a.name for a in node.names] if isinstance(node, ast.Import) else []
            )
            for name in names:
                assert not name.startswith(("tools.MediaCrawler", "MediaCrawler")), (path, name)
                if name == "mediacrawler_crawl" or name.startswith("scripts"):
                    assert path.relative_to(PACKAGE).as_posix() in {
                        "runtime/process.py", "application/collection.py",
                    }
                    assert name == "scripts.mediacrawler_crawl" and id(node) in type_nodes
                if path.relative_to(PACKAGE).parts[0] == "application" and name.startswith("trippostcollect.platforms."):
                    assert name == "trippostcollect.platforms.registry"


def test_exception_identity_and_shape():
    from trippostcollect.application import contracts
    from trippostcollect.artifacts import image_persistence

    assert image_persistence.ImagePersistenceError is contracts.ImagePersistenceError
    node = next(n for n in ast.parse((PACKAGE / "application/contracts.py").read_text()).body
                if isinstance(n, ast.ClassDef) and n.name == "ImagePersistenceError")
    assert ast.dump(node) == ast.dump(ast.parse("class ImagePersistenceError(ValueError):\n    pass\n").body[0])
    error = contracts.ImagePersistenceError("测试正文图片失败")
    assert str(error) == "测试正文图片失败"
    assert type(error).__name__ == "ImagePersistenceError"


def test_executor_ports_and_local_assembly():
    from trippostcollect.application.contracts import ExecutorPorts

    assert ExecutorPorts.__dataclass_params__.frozen
    assert [field.name for field in fields(ExecutorPorts)] == [
        "ensure_prerequisites", "run_bilibili_article_search",
    ]
    assert all(field.type == "Callable[..., Any]" for field in fields(ExecutorPorts))
    old = ast.parse((ROOT / "tests/fixtures/adapter_t11/mediacrawler_crawl.py.txt").read_text())
    entry = ast.parse((ROOT / "scripts/mediacrawler_crawl.py").read_text())

    def function(tree, name):
        return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)

    imports_by_name = {a.asname or a.name: f"{n.module}.{a.name}"
                       for n in entry.body if isinstance(n, ast.ImportFrom) for a in n.names}
    for name in ("_run_main", "run_platform", "_run_platform_without_policy"):
        before, after = function(old, name), function(entry, name)
        assert ast.dump(before.args) == ast.dump(after.args)
        assert ast.dump(before.returns) == ast.dump(after.returns)
        assert len(after.body) == 1 and isinstance(after.body[0], ast.Return)
        call = after.body[0].value
        assert isinstance(call.func, ast.Attribute) and call.func.attr == name
        assert imports_by_name[call.func.value.id] == "trippostcollect.application.collection"
        assert [arg.id for arg in call.args] == [arg.arg for arg in before.args.args]
        assert [(kw.arg, kw.value.id) for kw in call.keywords[:-1]] == [
            (arg.arg, arg.arg) for arg in before.args.kwonlyargs
        ]
        port = call.keywords[-1]
        assert port.arg == "ports" and isinstance(port.value, ast.Call)
        assert imports_by_name[port.value.func.id] == "trippostcollect.application.contracts.ExecutorPorts"
        assert not port.value.args
        assert [(kw.arg, kw.value.id) for kw in port.value.keywords] == [
            ("ensure_prerequisites", "ensure_prerequisites"),
            ("run_bilibili_article_search", "run_bilibili_article_search"),
        ]
    content = ast.parse((PACKAGE / "db/content.py").read_text())
    warmup = ast.parse((PACKAGE / "application/warmup.py").read_text())
    assert ast.dump(function(entry, "row_for_record")) == ast.dump(function(content, "row_for_record"))
    assert ast.dump(function(entry, "cookie_snapshot_path")) == ast.dump(function(warmup, "cookie_snapshot_path"))


@pytest.mark.parametrize("module", [
    "artifacts.paths", "artifacts.image_proxy", "artifacts.image_persistence",
    "artifacts.formal_images", "db.content", "application.collection", "application.reporting",
])
def test_independent_first_import(module):
    code = f"import trippostcollect.{module}; import sys; assert 'mediacrawler_crawl' not in sys.modules"
    if module in {"db.content", "artifacts.formal_images"}:
        code += "; assert 'trippostcollect.application.collection' not in sys.modules"
        code += "; assert 'trippostcollect.application.reporting' not in sys.modules"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "TRIPPOST_PROJECT_ROOT": str(ROOT)}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_constants_single_definition_and_same_object(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    executor = importlib.import_module("mediacrawler_crawl")
    old = ast.parse((ROOT / "tests/fixtures/adapter_t11/mediacrawler_crawl.py.txt").read_text())
    def assignments(tree):
        return {n.targets[0].id: n.value for n in tree.body if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name)}

    original = assignments(old)
    script_values = assignments(ast.parse((ROOT / "scripts/mediacrawler_crawl.py").read_text()))
    all_values = [script_values] + [assignments(ast.parse((PACKAGE / relative).read_text()))
                                    for relative in ALLOWED]
    for module, names in CONSTANTS.items():
        target = importlib.import_module("trippostcollect." + module)
        values = assignments(ast.parse((PACKAGE / (module.replace(".", "/") + ".py")).read_text()))
        for name in names:
            assert name not in script_values
            assert sum(name in values for values in all_values) == 1
            assert ast.dump(original[name]) == ast.dump(values[name])
            assert getattr(executor, name) is getattr(target, name)
