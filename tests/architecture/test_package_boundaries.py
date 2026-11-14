"""包与入口边界：保留代码不导入 fork、源码 checkout 的工作根、B站不是 worker、前置检查、正式入口 CLI、资源单一真源与 CI 分组。

构建与仓库外安装验收见 `tests/test_installation.py`（installation lane）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from support.formal_entries import FORMAL_ENTRIES, child_environment, help_command, help_environment


ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "scripts", ROOT / "scripts" / "dev"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

PACKAGE = ROOT / "src" / "trippostcollect"
# 已删除的 MediaCrawler fork 的顶层包名；保留代码不得导入。
FORK_TOPS = frozenset({
    "api", "base", "cache", "cmd_arg", "config", "constant", "database", "libs", "main",
    "media_platform", "model", "proxy", "recv_sms", "schema", "store", "tools", "var",
})
# 闸门工具目录不算保留生产代码。
GOVERNANCE_DIRS = ("scripts/dev/",)

# ---------- 通用扫描 ----------


def python_files(*relatives: str) -> list[Path]:
    files = []
    for relative in relatives:
        base = ROOT / relative
        candidates = [base] if base.is_file() else sorted(base.rglob("*.py"))
        files.extend(path for path in candidates if "__pycache__" not in path.parts)
    assert files, relatives
    return files


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def retained_production_files() -> list[Path]:
    files = python_files("src/trippostcollect", "scripts")
    return [path for path in files if not relative(path).startswith(GOVERNANCE_DIRS)]


def module_targets(tree: ast.AST):
    """静态导入、相对导入还原前的模块名，以及字符串导入（import_module/__import__/run_path）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module, node.lineno
        elif isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
            if name in {"import_module", "__import__"} and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    yield node.args[0].value, node.lineno


# ---------- 1. 保留代码不导入 fork ----------


def test_retained_package_and_scripts_import_no_fork_package() -> None:
    hits = []
    for path in retained_production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module, line in module_targets(tree):
            if module.split(".")[0] in FORK_TOPS:
                hits.append(f"{relative(path)}:{line} {module}")
        for node in ast.walk(tree):
            # 过渡装载点 _fork_bridge 把 fork 与 scripts 插入 sys.path；新入口不得再使用。
            if isinstance(node, ast.ImportFrom) and (
                node.module == "trippostcollect.platforms._fork_bridge"
                or (node.module == "trippostcollect.platforms" and any(a.name == "_fork_bridge" for a in node.names))
            ):
                hits.append(f"{relative(path)}:{node.lineno} 过渡装载 _fork_bridge")
    assert hits == []


def test_source_checkout_keeps_repository_root_without_explicit_work_root(tmp_path: Path) -> None:
    # 决策 9 的源码 checkout 一侧：未设 TRIPPOST_PROJECT_ROOT 时仍以仓库为根（安装态一侧见 install 用例）。
    environment = child_environment({}, tmp_path)
    result = subprocess.run(
        [sys.executable, "-P", "-c", "import trippostcollect.core.paths as p; print(p.PROJECT_ROOT)"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert Path(result.stdout.strip()) == ROOT


def test_formal_contract_path_stays_in_repository() -> None:
    # 决策 10：execution state 记录的契约路径与散列不变，仍指仓库 docs 下的唯一真源。
    paths = import_module("trippostcollect.core.paths")
    assert paths.FORMAL_CRAWL_CONTRACT == paths.PROJECT_ROOT / "docs" / "formal-crawl-contract.md"


# ---------- 2. B站进程内运行与前置检查 ----------

def test_bilibili_runs_in_process_and_is_not_a_worker(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import mediacrawler_crawl, crawl_runner\n"
        "from trippostcollect.platforms.bilibili import core, client, parser, login, signer\n"
        "from trippostcollect.platforms.entry import load_crawler\n"
        "try:\n    load_crawler('bili')\nexcept ValueError:\n    pass\nelse:\n    raise SystemExit('bili')\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", probe, str(ROOT / "scripts")],
        cwd=tmp_path, env=child_environment({}, tmp_path), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def test_prerequisites_verify_package_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    # 前置检查核对包内资源，不依赖源码树中的其他目录。
    crawl = import_module("mediacrawler_crawl")
    calls = []
    monkeypatch.setattr(crawl, "verify_package_resources", lambda: calls.append(True))
    crawl.ensure_prerequisites()
    assert calls == [True]


# ---------- 3. 正式入口 CLI ----------

def test_formal_entries_print_help(tmp_path: Path) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    environment = help_environment(tmp_path, workroot)
    failures = []
    for name in FORMAL_ENTRIES:
        result = subprocess.run(help_command(name), cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
        if result.returncode or result.stderr or not result.stdout.startswith("usage: "):
            failures.append([name, result.returncode, result.stderr[-300:]])
    assert failures == []


# ---------- 4. 资源单一真源 ----------

def test_generated_resources_are_not_hand_copied_into_source_tree() -> None:
    resources = PACKAGE / "resources"
    assert not (resources / "sql").exists()
    assert not (resources / "contracts").exists()
    assert sorted(path.name for path in (ROOT / "db").glob("*.sql")) == [
        "crawl_scheduler.sql", "ctf_captures.sql", "source_platforms.sql", "web_posts.sql", "xhs_control.sql",
    ]
    assert (ROOT / "docs" / "formal-crawl-contract.md").is_file()


# ---------- 5. CI 分组 ----------

def test_ci_fork_check_drops_upstream_bilibili_video_and_checks_root_five_site_assembly() -> None:
    source = (ROOT / "scripts" / "ci" / "run_matrix.py").read_text(encoding="utf-8")
    # 旧 Bili 视频主循环不属于正式能力；fork media_platform 整体不再作为 CI 静态导入对象。
    assert "media_platform." not in source
    # 新增根 article 与四站 worker 选站装配验收。
    assert "trippostcollect.platforms.entry" in source
    assert "trippostcollect.platforms.bilibili" in source


def test_ci_source_copy_includes_build_inputs() -> None:
    source = (ROOT / "scripts" / "ci" / "run_matrix.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fresh = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "fresh_source")
    copied = {node.value for node in ast.walk(fresh) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {"build_support.py", "MANIFEST.in"} <= copied
    workflow = (ROOT / ".github" / "workflows" / "macos-test-lanes.yml").read_text(encoding="utf-8")
    assert "'build_support.py'" in workflow and "'MANIFEST.in'" in workflow
