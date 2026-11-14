"""运行时行为层：注入依赖的要求与 runtime 包的导入方向。"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.runtime import behavior


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_guarded_behavior_requires_injected_wait(tmp_path):
    evidence_path = tmp_path / "behavior.json"
    with pytest.raises(
        RuntimeError, match="^xhs_guarded requires an injected xhs_search_ready and xhs_list_area$",
    ):
        await behavior.run_page_behavior(
            object(), platform_key="xhs", profile_name="xhs_guarded",
            evidence_path=evidence_path, write_evidence=write_evidence,
        )
    assert not evidence_path.exists()


@pytest.mark.asyncio
async def test_behavior_requires_injected_evidence_writer():
    with pytest.raises(TypeError, match="write_evidence"):
        await behavior.run_page_behavior(
            object(), platform_key="bilibili", evidence_path="unused.json",
        )
    with pytest.raises(TypeError, match="write_evidence"):
        await behavior.run_guarded_request_pause(
            evidence_path="unused.json", profile_name="xhs_guarded",
            stage="test", minimum=0, maximum=0,
        )


def test_runtime_import_direction():
    forbidden_packages = (
        "trippostcollect.application", "trippostcollect.artifacts",
        "trippostcollect.platforms",
    )
    forbidden_scripts = {
        "failure_classifier", "human_flow", "mediacrawler_behavior", "crawl_policy",
        "execution_state", "browser_runtime", "mediacrawler_crawl",
    }
    violations = []

    class Imports(ast.NodeVisitor):
        def visit_If(self, node):
            # 类型检查分支不在运行期执行；else 分支仍需检查。
            test = node.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                for child in node.orelse:
                    self.visit(child)
            else:
                self.generic_visit(node)

        def check(self, module, node):
            # 规格将 contracts 放在 application 下，runtime 可直接依赖此精确模块。
            if module == "trippostcollect.application.contracts":
                return
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_packages):
                violations.append((path.name, node.lineno, module))
            if module.split(".")[0] in forbidden_scripts or module.startswith("scripts."):
                violations.append((path.name, node.lineno, module))

        def visit_Import(self, node):
            for alias in node.names:
                self.check(alias.name, node)

        def visit_ImportFrom(self, node):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name(
                    "." * node.level + module,
                    ".".join(path.relative_to(ROOT / "src").with_suffix("").parts[:-1]),
                )
            if module == "trippostcollect.application.contracts":
                return
            if module != "trippostcollect.application":
                self.check(module, node)
            for alias in node.names:
                self.check(module + "." + alias.name, node)

    for path in sorted((ROOT / "src/trippostcollect/runtime").rglob("*.py")):
        Imports().visit(ast.parse(path.read_text(), filename=str(path)))
    assert violations == []
