"""小红书 T09：worker 入口装载、依赖方向与 mixin 归属的离线防线。"""

from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/trippostcollect"
PLATFORM = PACKAGE / "platforms/xhs"
FORK_TOPS = {
    "config", "tools", "media_platform", "store", "base", "var", "model", "constant", "proxy",
    "cmd_arg", "database", "cache", "libs", "api", "schema", "main", "recv_sms",
}
SCRIPT_MODULES = {path.stem for path in (ROOT / "scripts").glob("*.py")} | {"scripts"}
WORKER_CODES = ("wb", "dy", "zhihu", "xhs")
MIXIN_MODULES = {"session", "navigation", "login", "author", "detail", "media", "errors", "parser", "behavior"}


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
    commands = json.loads((ROOT / "tests/golden/t02_worker_commands.json").read_text())
    argv = list(commands["xhs_search_qrcode"]["cmd"][4:])
    return [str(tmp_path / "data") if item == "<TMP>/xhs_search_qrcode/xhs/data" else item for item in argv]


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
        "core", "client", "parser", "login", "models", "signer", "session", "navigation", "behavior",
        "author", "detail", "media", "repair", "manual_wait", "errors",
    }
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


def test_runtime_does_not_import_xhs_platform_or_scripts(tmp_path):
    allowed_root_xhs = {"trippostcollect.xhs.leases", "trippostcollect.xhs.runtime"}
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


def ledger_rows():
    rows = json.loads((ROOT / "docs/adapter-ledger/symbols.json").read_text())["rows"]
    return [row for row in rows if row["card"] == "T09"]


def test_crawler_methods_live_in_ledger_target_mixins():
    core = importlib.import_module("trippostcollect.platforms.xhs.core")
    crawler = core.XiaoHongShuCrawler
    rows = [
        row for row in ledger_rows()
        if row["file"].endswith("media_platform/xhs/core.py") and row["qualname"].startswith("XiaoHongShuCrawler.")
        and row["target"].startswith("platforms/xhs/")
    ]
    assert len(rows) == 69
    for row in rows:
        name = row["qualname"].split(".", 1)[1]
        owner = next(cls for cls in crawler.__mro__ if name in vars(cls))
        expected = "trippostcollect." + row["target"][:-3].replace("/", ".")
        assert owner.__module__ == expected, (name, owner, expected)
    # 每个目标模块一个 mixin，作为组合类的直接基类；排在前面的 mixin 只继承 object。
    mixins = [base for base in crawler.__bases__ if base.__module__ != core.__name__]
    assert {base.__module__.rsplit(".", 1)[-1] for base in mixins} == {
        row["target"][len("platforms/xhs/"):-3] for row in rows if row["target"] != "platforms/xhs/core.py"
    }
    for base in mixins:
        assert base.__module__.rsplit(".", 1)[-1] in MIXIN_MODULES
        assert base.__bases__ == (object,), base
    tree = ast.parse((PLATFORM / "core.py").read_text())
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("trippostcollect.platforms.xhs."):
            assert node.level == 0
            imported.update({alias.asname or alias.name: node.module for alias in node.names})
    (class_node,) = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "XiaoHongShuCrawler"]
    moved = {row["qualname"].split(".", 1)[1] for row in rows if row["target"] != "platforms/xhs/core.py"}
    for base in class_node.bases[: len(mixins)]:
        assert isinstance(base, ast.Name), ast.unparse(base)
        assert imported.get(base.id, "").startswith("trippostcollect.platforms.xhs."), base.id
    defined = {node.name for node in class_node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not defined & moved, sorted(defined & moved)
    # 原实例属性名保持，fork 测试与旧桥仍按这些名字替换。
    for name in ("_popup_sleep", "_popup_monotonic", "_get_manual_wait_budget", "_run_browser_session",
                 "launch_browser_with_cdp", "create_xhs_client"):
        assert callable(getattr(crawler, name))


def test_old_locations_keep_no_second_implementation():
    """旧 scripts 位置只能薄转发或重导出，不保留第二份权威定义。"""
    moved = {}
    for row in ledger_rows():
        if (row["file"].startswith("scripts/") and "." not in row["qualname"]
                and row["target"].startswith(("platforms/xhs/", "artifacts/", "xhs/"))):
            moved.setdefault(row["file"], set()).add(row["qualname"])
    assert set(moved) == {
        "scripts/mediacrawler_behavior.py", "scripts/mediacrawler_export_entrypoint.py", "scripts/mediacrawler_crawl.py",
    }
    for relative, names in moved.items():
        tree = ast.parse((ROOT / relative).read_text())
        definitions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        exempt = REPAIR_FLAG_HOOK if relative == "scripts/mediacrawler_export_entrypoint.py" else None
        assert not (set(definitions) & names) - {exempt}, (relative, sorted(set(definitions) & names))
        if exempt is not None:
            # 与 T05/T06 先例一致：旧桥 hook 只读取开关并在 entry 上置标志，台账保持 pending。
            assert exempt in definitions
            check_repair_flag_hook(definitions[exempt])


REPAIR_FLAG_HOOK = "install_xhs_repair_resilience"


def check_repair_flag_hook(node):
    """旧桥修复 hook 的形态约束：不含修复逻辑、不替换 crawler 方法、不按模块路径字符串查异常。"""
    assert isinstance(node, ast.FunctionDef) and not node.args.args and not node.decorator_list
    readers = set()
    flag_assignments = 0
    for child in ast.walk(node):
        if child is node:
            continue
        assert not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                                      ast.Import, ast.Await, ast.For, ast.While, ast.With, ast.Try)), ast.dump(child)
        if isinstance(child, ast.ImportFrom):
            assert child.module in {"trippostcollect.application.worker_inputs", "trippostcollect.platforms"}
            if child.module == "trippostcollect.platforms":
                assert [alias.name for alias in child.names] == ["entry"]
            else:
                readers.update(alias.asname or alias.name for alias in child.names)
        elif isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            assert isinstance(child, ast.Assign) and len(targets) == 1
            target = targets[0]
            assert isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            assert target.value.id == "entry" and target.attr.startswith("_xhs")
            assert isinstance(child.value, ast.Constant) and child.value.value is True
            flag_assignments += 1
        elif isinstance(child, ast.Call):
            function = child.func.func if isinstance(child.func, ast.Call) else child.func
            assert isinstance(function, ast.Name) and function.id in readers, ast.dump(child)
        elif isinstance(child, ast.Name):
            assert child.id not in {"sys", "setattr", "gather", "Semaphore", "os"}, child.id
        elif isinstance(child, ast.Attribute):
            assert child.attr not in {"modules", "get_specified_notes", "XiaoHongShuCrawler", "__dict__"}, child.attr
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            assert "media_platform" not in child.value and "xhs_core" not in child.value
    assert readers and flag_assignments == 1


def test_formal_xhs_accumulator_publishes_through_worker_event_exit():
    """T14：原 test_adapter_t09_bridge 中正式装配一侧的断言；旧桥一侧随 E 在 T14-C 删除。"""
    from trippostcollect.application.events import append_worker_execution_event
    from trippostcollect.application.worker_inputs import worker_config
    from trippostcollect.platforms import entry

    accumulator = entry.xhs_dependencies(worker_config(), repair=False)["ports"].accumulator_factory()
    assert accumulator.event_sink is append_worker_execution_event
