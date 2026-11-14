"""小红书 T09：worker 入口装载、依赖方向与 mixin 归属的离线防线。"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from support import worker_scenarios


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/trippostcollect"
PLATFORM = PACKAGE / "platforms/xhs"
FORK_TOPS = {
    "config", "tools", "media_platform", "store", "base", "var", "model", "constant", "proxy",
    "cmd_arg", "database", "cache", "libs", "api", "schema", "main", "recv_sms",
}
SCRIPT_MODULES = {path.stem for path in (ROOT / "scripts").glob("*.py")} | {"scripts"}
WORKER_CODES = ("wb", "dy", "zhihu", "xhs")
# 平台层只可依赖根侧父子共用的格式定义，不得依赖租约、发现等控制面。
# #94：互动计数取证的逐篇证据与子摘要汇总共用 xhs.engagement（只依赖标准库与 records.formal）。
XHS_ROOT_MODULES_ALLOWED = {
    "trippostcollect.xhs.engagement", "trippostcollect.xhs.operator_wait", "trippostcollect.xhs.search_target",
}


def resolved_imports(path: Path, package: str):
    """包含函数内与 TYPE_CHECKING 块；相对导入还原为绝对模块。"""
    tree = ast.parse(path.read_text())
    type_checking = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            type_checking.update(id(child) for item in node.body for child in ast.walk(item))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, id(node) in type_checking
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            yield node, module, id(node) in type_checking


def worker_argv(tmp_path: Path) -> list[str]:
    argv = worker_scenarios.worker_argv("xhs_search_qrcode", tmp_path)
    return [str(tmp_path / "data") if item == f"{tmp_path}/xhs_search_qrcode/xhs/data" else item for item in argv]


def run_child(code: str, tmp_path: Path, **env) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TRIPPOSTCOLLECT_")}
    environment.update(env)
    return subprocess.run(
        [sys.executable, "-P", "-c", code], cwd=tmp_path, env=environment, text=True, capture_output=True,
    )


@pytest.mark.parametrize("repair", ["0", "1"])
def test_xhs_worker_loads_root_platform_without_fork_xhs(tmp_path, repair):
    argv = worker_argv(tmp_path)
    code = (
        "import sys, json\n"
        "from trippostcollect.platforms.entry import configure, install_hooks, load_crawler\n"
        f"configure({argv!r})\ninstall_hooks()\n"
        "cls = load_crawler('xhs'); crawler = cls()\n"
        "from trippostcollect.platforms.xhs.core import XiaoHongShuCrawler as Root\n"
        "assert issubclass(cls, Root), cls\n"
        "assert cls.__module__.startswith('trippostcollect.platforms.xhs') or cls.__module__ == 'trippostcollect.platforms.entry', cls.__module__\n"
        "assert cls.__name__ == 'XiaoHongShuCrawler'\n"
        "loaded = sorted(n for n in sys.modules if n in ('media_platform.xhs', 'store.xhs')\n"
        "                or n.startswith(('media_platform.xhs.', 'store.xhs.')))\n"
        "assert not loaded, loaded\n"
        "assert not hasattr(cls, '_trippostcollect_repair_resilience')\n"
        "print(json.dumps(sorted(n for n in sys.modules if n.startswith('trippostcollect.platforms.xhs'))))\n"
    )
    result = run_child(code, tmp_path, TRIPPOSTCOLLECT_XHS_REPAIR=repair)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "trippostcollect.platforms.xhs.core" in json.loads(result.stdout.splitlines()[-1])


def test_worker_platforms_never_import_fork_media_platform(tmp_path):
    argv = worker_argv(tmp_path)
    code = (
        "import sys, json\n"
        "from trippostcollect.platforms.entry import configure, install_hooks, load_crawler\n"
        f"configure({argv!r})\ninstall_hooks()\n"
        f"names = [load_crawler(code).__name__ for code in {WORKER_CODES!r}]\n"
        "fork = sorted(n for n in sys.modules if n == 'media_platform' or n.startswith('media_platform.')\n"
        "              or n == 'store.xhs' or n.startswith('store.xhs.'))\n"
        "assert not fork, fork\n"
        "try:\n    load_crawler('bili')\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError('bili')\n"
        "print(json.dumps(names))\n"
    )
    result = run_child(code, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.splitlines()[-1]) == [
        "WeiboCrawler", "DouYinCrawler", "ZhihuCrawler", "XiaoHongShuCrawler",
    ]


def test_platform_xhs_dependency_direction():
    files = sorted(PLATFORM.glob("*.py"))
    assert {path.stem for path in files} >= {
        "core", "parser", "login", "models", "session", "navigation", "behavior",
        "author", "detail", "media", "repair", "manual_wait", "errors",
    }
    # #71：httpx 客户端与 xhshow 本地签名整体删除。
    assert not {"client", "signer"} & {path.stem for path in files}
    for path in files:
        for _, module, _ in resolved_imports(path, f"trippostcollect.platforms.xhs.{path.stem}"):
            top = module.split(".")[0]
            assert top not in FORK_TOPS, (path.name, module)
            assert top not in SCRIPT_MODULES and not top.startswith("mediacrawler_"), (path.name, module)
            assert not module.startswith(("trippostcollect.artifacts", "trippostcollect.db")), (path.name, module)
            if module.startswith("trippostcollect.application"):
                assert module == "trippostcollect.application.contracts", (path.name, module)
            if module.startswith("trippostcollect.platforms"):
                assert module.startswith("trippostcollect.platforms.xhs"), (path.name, module)
            if module == "trippostcollect.xhs" or module.startswith("trippostcollect.xhs."):
                assert module in XHS_ROOT_MODULES_ALLOWED, (path.name, module)


def test_xhshow_is_no_longer_a_dependency():
    """#71：小红书不再本地签名，xhshow 与其唯一依赖 pycryptodome 不在依赖声明与锁文件中。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["dependencies"] + [item for group in project.get("optional-dependencies", {}).values() for item in group]
    assert not [item for item in declared if item.lower().startswith(("xhshow", "pycryptodome"))]
    locked = {package["name"] for package in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]}
    assert not {"xhshow", "pycryptodome"} & locked


def test_search_target_imports_only_standard_library():
    path = PACKAGE / "xhs/search_target.py"
    for _, module, _ in resolved_imports(path, "trippostcollect.xhs.search_target"):
        top = module.split(".")[0]
        assert top == "__future__" or top in sys.stdlib_module_names, module


def test_runtime_does_not_import_xhs_platform_or_scripts(tmp_path):
    # #75：人工等待诊断的格式常量只依赖标准库，父层读取与 child 写入共用这一处定义。
    allowed_root_xhs = {
        "trippostcollect.xhs.leases",
        "trippostcollect.xhs.runtime",
        "trippostcollect.xhs.operator_wait",
    }
    for path in sorted((PACKAGE / "runtime").glob("*.py")):
        for _, module, guarded in resolved_imports(path, f"trippostcollect.runtime.{path.stem}"):
            if guarded:
                continue
            top = module.split(".")[0]
            assert top not in SCRIPT_MODULES and not top.startswith("mediacrawler_"), (path.name, module)
            assert not module.startswith("trippostcollect.platforms"), (path.name, module)
            if module.startswith("trippostcollect.xhs"):
                assert module in allowed_root_xhs, (path.name, module)
            if module.startswith("trippostcollect.application"):
                assert module == "trippostcollect.application.contracts", (path.name, module)
    # 监督 reporter 只在类型检查下从根 xhs/supervision 引用，不再指向 scripts。
    for relative in ("runtime/process.py", "application/collection.py"):
        reporters = [
            (module, guarded)
            for node, module, guarded in resolved_imports(PACKAGE / relative, "trippostcollect." + relative[:-3].replace("/", "."))
            if isinstance(node, ast.ImportFrom) and "XhsSupervisorRuntimeReporter" in {alias.name for alias in node.names}
        ]
        assert reporters == [("trippostcollect.xhs.supervision", True)], (relative, reporters)
    modules = sorted(f"trippostcollect.runtime.{path.stem}" for path in (PACKAGE / "runtime").glob("*.py")
                     if path.stem != "__init__")
    code = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}:\n    importlib.import_module(name)\n"
        # 现有 xhs.leases→accounts→db.bootstrap 链会带入只读站点表 platforms.registry，不属于平台实现。
        "bad = sorted(n for n in sys.modules if n.startswith(('trippostcollect.platforms.', 'mediacrawler_', 'scripts'))\n"
        "             and n != 'trippostcollect.platforms.registry' or n == 'trippostcollect.xhs.supervision')\n"
        "print(json.dumps(bad))\n"
    )
    result = run_child(code, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.splitlines()[-1]) == []


def test_formal_xhs_accumulator_publishes_through_worker_event_exit():
    """T14：原 test_adapter_t09_bridge 中正式装配一侧的断言；旧桥一侧随 E 在 T14-C 删除。"""
    from trippostcollect.application.events import append_worker_execution_event
    from trippostcollect.application.worker_inputs import worker_config
    from trippostcollect.platforms import entry

    accumulator = entry.xhs_dependencies(worker_config(), repair=False)["ports"].accumulator_factory()
    assert accumulator.event_sink is append_worker_execution_event
