"""策略与行为迁入包后的依赖边界及旧入口契约。"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from trippostcollect.application import failures, policy
from trippostcollect.artifacts.evidence import write_evidence
from trippostcollect.records import text_signals
from trippostcollect.runtime import behavior, human_flow


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_forwarders_preserve_function_identity():
    for name, implementation in (
        ("crawl_policy", policy),
        ("failure_classifier", failures),
        ("human_flow", human_flow),
    ):
        legacy = importlib.import_module(name)
        for symbol, value in vars(implementation).items():
            if inspect.isfunction(value) or inspect.isclass(value):
                assert getattr(legacy, symbol) is value, (name, symbol)
    for name in (
        "_xhs_stable_sms_terminal_reason", "_xhs_sms_terminal_reason",
        "is_xhs_sms_terminal_text", "XHS_LEGACY_LOGIN_TERMINAL_PATTERNS",
        "_XHS_SMS_CONTEXT", "_XHS_SMS_VERIFICATION", "_XHS_SMS_PARAMETER_ERROR",
        "_XHS_DAILY", "_XHS_LIMIT", "XHS_SMS_PARAMETER_TERMINAL_PATTERNS",
        "XHS_SMS_DAILY_LIMIT_PATTERNS", "XHS_SMS_FREQUENCY_PATTERNS",
    ):
        assert getattr(failures, name) is getattr(text_signals, name)
    assert behavior.is_xhs_sms_terminal_text is text_signals.is_xhs_sms_terminal_text


def test_fork_dynamic_imports_and_behavior_injection(tmp_path):
    # 子进程只显式添加 scripts；包由当前源码副本的环境提供。
    program = textwrap.dedent('''
        import asyncio
        import importlib
        from pathlib import Path
        import sys

        sys.path.insert(0, sys.argv[1])
        from human_flow import install_runtime_hints
        from mediacrawler_behavior import (
            run_page_behavior, run_guarded_request_pause,
            run_xhs_continuity_behavior, run_xhs_api_captcha_verification,
            visible_page_state, record_xhs_platform_security_limit,
            run_xhs_post_interaction,
        )
        from trippostcollect.runtime import behavior, human_flow
        from trippostcollect.artifacts.evidence import write_evidence

        legacy = importlib.import_module("mediacrawler_behavior")
        assert install_runtime_hints is human_flow.install_runtime_hints
        assert visible_page_state is behavior.visible_page_state
        assert legacy.write_evidence is write_evidence
        for function in (
            run_page_behavior, run_guarded_request_pause,
            run_xhs_continuity_behavior, run_xhs_api_captcha_verification,
            record_xhs_platform_security_limit, run_xhs_post_interaction,
        ):
            assert function is getattr(legacy, function.__name__)

        calls = []
        class FakePage:
            url = "https://www.xiaohongshu.com/search_result?keyword=青岛"
            frames = []
            main_frame = None

            def locator(self, selector):
                assert selector == "body"
                return self

            async def inner_text(self, **kwargs):
                return "青岛图文搜索结果"

            async def evaluate(self, expression):
                if "navigator.webdriver" in expression:
                    return {
                        "webdriver": None, "languages": ["zh-CN"],
                        "platform": "test", "user_agent": "test",
                        "visibility_state": "visible",
                        "viewport": {"width": 100, "height": 100},
                    }
                return False

            async def screenshot(self, **kwargs):
                calls.append("screenshot")

        async def ready(page, events):
            assert isinstance(page, FakePage)
            assert events == []
            calls.append("xhs_search_ready")
            return {"ready": True}

        async def dwell(page, profile, log):
            calls.append("dwell")
            log.extend([
                {"event": "pause"}, {"event": "mouse_moves"},
                {"event": "human_scroll_complete", "effective_passes": 1},
            ])

        def writer(path, evidence):
            calls.append("write_evidence")
            assert evidence["status"] == "completed"
            write_evidence(path, evidence)

        legacy.wait_for_xhs_search_ready = ready
        legacy.write_evidence = writer
        behavior.dwell_on_list = dwell
        evidence_path = Path(sys.argv[2]) / "behavior.json"
        evidence = asyncio.run(run_page_behavior(
            FakePage(), platform_key="xhs", evidence_path=evidence_path,
            profile_name="xhs_guarded",
        ))
        assert calls == ["xhs_search_ready", "dwell", "screenshot", "write_evidence"]
        assert behavior.load_behavior_evidence(evidence_path) == evidence
        event = asyncio.run(run_guarded_request_pause(
            evidence_path=evidence_path, profile_name="xhs_guarded",
            stage="test", minimum=0, maximum=0,
        ))
        assert calls[-1] == "write_evidence"
        assert calls.count("write_evidence") == 2
        assert behavior.load_behavior_evidence(evidence_path)["request_pacing_events"] == [event]
    ''')
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-P", "-c", program, str(ROOT / "scripts"), str(tmp_path)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_guarded_behavior_requires_injected_wait(tmp_path):
    evidence_path = tmp_path / "behavior.json"
    with pytest.raises(
        RuntimeError, match="^xhs_guarded requires an injected xhs_search_ready$",
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
