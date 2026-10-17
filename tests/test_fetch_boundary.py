"""#74 静态旁路检查：纳入访问门禁的平台只能经页面取数通道取得平台数据。

规则（任一命中即失败）：
1. 禁止导入浏览器外 HTTP 客户端、JS 执行器、本地签名库与项目的 HTTP/Cookie 运行时模块；
2. 禁止 Playwright 请求接口、路由、主动等待响应、响应事件监听和零参数读体；
3. `evaluate` 一类调用只允许出现在平台的 `page_scripts.py`；
4. 页面脚本中禁止主动发起请求、动态加载或跳转的写法；扫描平台 `page_scripts.py` 的全部字符串，
   以及平台会调用的 runtime 模块中作为第一个参数传给 evaluate / evaluate_handle / add_init_script
   的字符串常量（直接字面量或模块级常量名）；
5. `page_scripts.py` 以两个登记表区分脚本：`DOM_SCRIPTS` 只读 DOM 几何与文本，不得含
   `__INITIAL_STATE__`；`STATE_SCRIPTS` 的脚本名只能作为 `read_state(` 的参数被引用，登记表本身只能
   作为 `state_scripts=` 交给通道，不得被下标取用。

盲区：`runtime/browser.py` 以 `add_init_script(path=...)` 注入的 stealth.min.js 是文件而非字符串常量，
不在本检查范围内，归 #72。小红书正式轮自 #83 起不注入任何页面脚本，由
`tests/platforms/xhs/test_xhs_login_contract.py::test_startup_injects_no_page_scripts` 覆盖。
`runtime/page_readiness.py` 只供页面证据使用，不在扫描清单内。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/trippostcollect"
# 平台 → 包内目录。抖音、知乎、B站在各自改造批次加入；微博见 #73。
GUARDED_PLATFORMS: dict[str, str] = {"xhs": "platforms/xhs"}
# 平台会调用、且向页面传入脚本字符串的 runtime 模块；平台新增依赖时同步加入。
RUNTIME_SCRIPT_MODULES: dict[str, tuple[str, ...]] = {
    "xhs": ("runtime/behavior.py", "runtime/human_flow.py", "runtime/page_capture.py"),
}
# 每平台一个显式白名单：(包内相对路径, 规则编号, 命中详情)。条目必须仍然命中，过期即失败。
WHITELISTS: dict[str, frozenset[tuple[str, str, str]]] = {
    "xhs": frozenset(),
}

FORBIDDEN_MODULES = (
    "httpx",
    "urllib.request",
    "urllib3",
    "requests",
    "aiohttp",
    "execjs",
    "xhshow",
    "trippostcollect.runtime.http",
    "trippostcollect.runtime.cookies",
)
REQUEST_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "fetch"})
FORBIDDEN_NAMES = frozenset({"APIRequestContext", "new_context"})
FORBIDDEN_METHODS = frozenset({"route", "unroute", "expect_response", "expect_request"})
FORBIDDEN_EVENTS = frozenset({"response", "request", "requestfinished", "requestfailed"})
BODY_READERS = frozenset({"body", "json", "text", "content"})
SCRIPT_CALLS = frozenset({"evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all",
                          "add_init_script"})
RUNTIME_SCRIPT_CALLS = frozenset({"evaluate", "evaluate_handle", "add_init_script"})
SCRIPT_FORBIDDEN = (
    ("fetch(", re.compile(r"\bfetch\s*\(")),
    ("XMLHttpRequest", re.compile(r"XMLHttpRequest")),
    ("sendBeacon", re.compile(r"sendBeacon")),
    ("WebSocket", re.compile(r"WebSocket")),
    ("EventSource", re.compile(r"EventSource")),
    ("import(", re.compile(r"\bimport\s*\(")),
    ("new Image", re.compile(r"\bnew\s+Image\b")),
    (".src=", re.compile(r"\.src\s*=(?!=)")),
    ("createElement(", re.compile(r"createElement\s*\(")),
    ("location.href=", re.compile(r"location\s*\.\s*href\s*=(?!=)")),
    ("location.assign", re.compile(r"location\s*\.\s*assign")),
    ("location.replace", re.compile(r"location\s*\.\s*replace")),
    ("window.open", re.compile(r"window\s*\.\s*open\b")),
)
PAGE_SCRIPTS = "page_scripts.py"


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def key(self) -> tuple[str, str, str]:
        return self.path, self.rule, self.detail


def _forbidden_module(name: str) -> str | None:
    return next(
        (module for module in FORBIDDEN_MODULES if name == module or name.startswith(module + ".")),
        None,
    )


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def check_module(source: str, relative: str) -> list[Violation]:
    """规则 1–3：导入、请求接口与读体、脚本执行位置。"""
    tree = ast.parse(source)
    is_page_scripts = Path(relative).name == PAGE_SCRIPTS
    found: list[Violation] = []

    def hit(node: ast.AST, rule: str, detail: str) -> None:
        found.append(Violation(relative, getattr(node, "lineno", 0), rule, detail))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = _forbidden_module(alias.name)
                if module:
                    hit(node, "R1", module)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            module = _forbidden_module(node.module)
            if module:
                hit(node, "R1", module)
            for alias in node.names:
                module = _forbidden_module(f"{node.module}.{alias.name}")
                if module:
                    hit(node, "R1", module)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            hit(node, "R2", node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            hit(node, "R2", node.attr)
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        func = node.func
        if name in {"import_module", "__import__"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                module = _forbidden_module(first.value)
                if module:
                    hit(node, "R1", module)
        if not isinstance(func, ast.Attribute):
            continue
        if (
            func.attr in REQUEST_VERBS
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "request"
        ):
            hit(node, "R2", f"request.{func.attr}")
        if func.attr in FORBIDDEN_METHODS:
            hit(node, "R2", func.attr)
        if func.attr in {"on", "once"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in FORBIDDEN_EVENTS:
                hit(node, "R2", f"{func.attr}:{first.value}")
        if func.attr in BODY_READERS and not node.args and not node.keywords:
            hit(node, "R2", f"{func.attr}()")
        if func.attr in SCRIPT_CALLS and not is_page_scripts:
            hit(node, "R3", func.attr)
    return found


def _script_violations(text: str, relative: str, line: int) -> list[Violation]:
    return [
        Violation(relative, line, "R4", label)
        for label, pattern in SCRIPT_FORBIDDEN
        if pattern.search(text)
    ]


def check_page_scripts(source: str, relative: str) -> list[Violation]:
    """规则 4（平台脚本全部字符串）与规则 5 中 DOM 脚本不得读页面状态。"""
    tree = ast.parse(source)
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(_script_violations(node.value, relative, node.lineno))
    dom = _registry(tree, "DOM_SCRIPTS")
    state = _registry(tree, "STATE_SCRIPTS")
    if dom is None or state is None:
        found.append(Violation(relative, 0, "R5", "missing DOM_SCRIPTS/STATE_SCRIPTS registry"))
        return found
    for name, (script, line) in dom.items():
        if "__INITIAL_STATE__" in script:
            found.append(Violation(relative, line, "R5", f"dom:{name}:__INITIAL_STATE__"))
    return found


def _registry(tree: ast.Module, name: str) -> dict[str, tuple[str, int]] | None:
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name and isinstance(value, ast.Dict):
            entries: dict[str, tuple[str, int]] = {}
            for key, item in zip(value.keys, value.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    return None
                if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                    return None
                entries[key.value] = (item.value, item.lineno)
            return entries
    return None


def state_script_names(page_scripts_source: str) -> frozenset[str]:
    return frozenset(_registry(ast.parse(page_scripts_source), "STATE_SCRIPTS") or {})


def check_state_references(source: str, relative: str, state_names: frozenset[str]) -> list[Violation]:
    """规则 5：状态脚本名只作为 read_state 的脚本名参数；登记表只作为 state_scripts= 交给通道。"""
    tree = ast.parse(source)
    allowed: set[int] = set()
    is_page_scripts = Path(relative).name == PAGE_SCRIPTS
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "read_state":
            candidates = list(node.args[1:2]) + [kw.value for kw in node.keywords if kw.arg == "script_name"]
            allowed.update(id(item) for item in candidates)
        if isinstance(node, ast.Call):
            allowed.update(id(kw.value) for kw in node.keywords if kw.arg == "state_scripts")
        if is_page_scripts and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "STATE_SCRIPTS" for target in targets):
                allowed.update(id(item) for item in ast.walk(node))
    found: list[Violation] = []
    for node in ast.walk(tree):
        if id(node) in allowed:
            continue
        if isinstance(node, ast.Constant) and node.value in state_names:
            found.append(Violation(relative, node.lineno, "R5", f"state:{node.value}"))
        elif isinstance(node, ast.Name) and node.id == "STATE_SCRIPTS":
            found.append(Violation(relative, node.lineno, "R5", "STATE_SCRIPTS"))
        elif isinstance(node, ast.Attribute) and node.attr == "STATE_SCRIPTS":
            found.append(Violation(relative, node.lineno, "R5", "STATE_SCRIPTS"))
    return found


def _module_strings(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.targets[0].id] = node.value.value
    return constants


def runtime_scripts(source: str) -> list[tuple[int, str]]:
    """runtime 模块中作为第一个参数传给脚本调用的字符串常量（字面量或模块级常量名）。"""
    tree = ast.parse(source)
    constants = _module_strings(tree)
    scripts: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) in RUNTIME_SCRIPT_CALLS):
            continue
        candidates = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg in {"expression", "script"}]
        for item in candidates:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                scripts.append((item.lineno, item.value))
            elif isinstance(item, ast.Name) and item.id in constants:
                scripts.append((item.lineno, constants[item.id]))
    return scripts


def check_runtime_scripts(source: str, relative: str) -> list[Violation]:
    return [
        violation
        for line, text in runtime_scripts(source)
        for violation in _script_violations(text, relative, line)
    ]


def platform_violations(platform: str) -> list[Violation]:
    root = PACKAGE / GUARDED_PLATFORMS[platform]
    sources = {
        path.relative_to(PACKAGE).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
    }
    page_scripts = f"{GUARDED_PLATFORMS[platform]}/{PAGE_SCRIPTS}"
    found: list[Violation] = []
    if page_scripts not in sources:
        return [Violation(page_scripts, 0, "R5", "missing page_scripts.py")]
    state_names = state_script_names(sources[page_scripts])
    for relative, source in sources.items():
        found.extend(check_module(source, relative))
        found.extend(check_state_references(source, relative, state_names))
    found.extend(check_page_scripts(sources[page_scripts], page_scripts))
    for relative in RUNTIME_SCRIPT_MODULES[platform]:
        found.extend(check_runtime_scripts((PACKAGE / relative).read_text(encoding="utf-8"), relative))
    return found


def test_guarded_platforms_have_no_fetch_bypass():
    assert set(GUARDED_PLATFORMS) <= set(RUNTIME_SCRIPT_MODULES)
    assert set(GUARDED_PLATFORMS) <= set(WHITELISTS)
    for platform in GUARDED_PLATFORMS:
        found = platform_violations(platform)
        whitelist = WHITELISTS[platform]
        unexpected = [item for item in found if item.key() not in whitelist]
        assert not unexpected, unexpected
        stale = whitelist - {item.key() for item in found}
        assert not stale, f"白名单条目已不再命中：{sorted(stale)}"


def test_runtime_script_module_lists_resolve_to_real_scripts():
    for modules in RUNTIME_SCRIPT_MODULES.values():
        found = 0
        for relative in modules:
            found += len(runtime_scripts((PACKAGE / relative).read_text(encoding="utf-8")))
        assert found, "清单中的 runtime 模块应能解析出传给页面的脚本"


# 自测：合成片段的正反例，保证每条规则本身生效。


def details(found: list[Violation]) -> list[tuple[str, str]]:
    return [(item.rule, item.detail) for item in found]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("import httpx", "httpx"),
        ("import urllib.request", "urllib.request"),
        ("from urllib import request", "urllib.request"),
        ("from urllib.request import urlopen", "urllib.request"),
        ("import urllib3", "urllib3"),
        ("import requests", "requests"),
        ("from aiohttp import ClientSession", "aiohttp"),
        ("import execjs", "execjs"),
        ("from xhshow import Xhshow", "xhshow"),
        ("from trippostcollect.runtime import cookies", "trippostcollect.runtime.cookies"),
        ("from trippostcollect.runtime.http import build", "trippostcollect.runtime.http"),
        ("import trippostcollect.runtime.cookies as c", "trippostcollect.runtime.cookies"),
        ("importlib.import_module('httpx')", "httpx"),
        ("__import__('requests')", "requests"),
    ],
)
def test_rule1_forbidden_imports(line, expected):
    assert ("R1", expected) in details(check_module(line + "\n", "platforms/x/a.py"))


@pytest.mark.parametrize(
    "line",
    [
        "import json",
        "from urllib.parse import urlsplit",
        "from trippostcollect.runtime.page_capture import PageCaptureChannel",
        "from trippostcollect.runtime import behavior",
        "import importlib\nimportlib.import_module('json')",
    ],
)
def test_rule1_allows_other_imports(line):
    assert check_module(line + "\n", "platforms/x/a.py") == []


@pytest.mark.parametrize(
    "line,expected",
    [
        ("page.request.get(url)", "request.get"),
        ("context.request.post(url, data=b'')", "request.post"),
        ("self.page.request.fetch(url)", "request.fetch"),
        ("x = APIRequestContext", "APIRequestContext"),
        ("ctx = await browser.new_context()", "new_context"),
        ("await context.route('**/*', handler)", "route"),
        ("await context.unroute('**/*')", "unroute"),
        ("async with page.expect_response(p) as info: pass", "expect_response"),
        ("async with page.expect_request(p) as info: pass", "expect_request"),
        ("page.on('response', handler)", "on:response"),
        ("page.once('requestfinished', handler)", "once:requestfinished"),
        ("context.on('request', handler)", "on:request"),
        ("page.on('requestfailed', handler)", "on:requestfailed"),
        ("data = await response.body()", "body()"),
        ("data = await response.json()", "json()"),
        ("data = await response.text()", "text()"),
        ("html = await page.content()", "content()"),
    ],
)
def test_rule2_request_interfaces_and_body_reads(line, expected):
    source = f"async def run():\n    {line}\n"
    assert ("R2", expected) in details(check_module(source, "platforms/x/a.py"))


@pytest.mark.parametrize(
    "line",
    [
        "context.on('page', handler)",
        "page.on('close', handler)",
        "value = captured.json",
        "value = item.request_json",
        "text = await locator.inner_text()",
        "value = response.body(1)",
        "await channel.wait_for('x.feed', after_seq=1, timeout=3)",
        "self.request_count.get('x')",
    ],
)
def test_rule2_allows_capture_channel_usage(line):
    source = f"async def run():\n    {line}\n"
    assert check_module(source, "platforms/x/a.py") == []


@pytest.mark.parametrize("call", ["evaluate", "evaluate_handle", "eval_on_selector", "add_init_script"])
def test_rule3_scripts_only_in_page_scripts(call):
    source = f"async def run(page):\n    await page.{call}('() => 1')\n"
    assert ("R3", call) in details(check_module(source, "platforms/x/core.py"))
    assert check_module(source, "platforms/x/page_scripts.py") == []


PAGE_SCRIPTS_OK = '''
DOM_SCRIPTS = {"cards": "() => document.querySelectorAll('section').length"}
STATE_SCRIPTS = {"creator_page_data": "() => window.__INITIAL_STATE__.user"}


async def visible_cards(page):
    return await page.evaluate(DOM_SCRIPTS["cards"])
'''


def test_rule4_and_rule5_accept_clean_page_scripts():
    assert check_page_scripts(PAGE_SCRIPTS_OK, "platforms/x/page_scripts.py") == []
    assert state_script_names(PAGE_SCRIPTS_OK) == {"creator_page_data"}
    assert check_state_references(PAGE_SCRIPTS_OK, "platforms/x/page_scripts.py",
                                  frozenset({"creator_page_data"})) == []


@pytest.mark.parametrize(
    "script,label",
    [
        ("() => fetch('/api/x')", "fetch("),
        ("() => window.fetch ('/api/x')", "fetch("),
        ("() => new XMLHttpRequest()", "XMLHttpRequest"),
        ("() => navigator.sendBeacon('/t', '')", "sendBeacon"),
        ("() => new WebSocket('wss://x')", "WebSocket"),
        ("() => new EventSource('/s')", "EventSource"),
        ("() => import('/m.js')", "import("),
        ("() => { const i = new  Image(); }", "new Image"),
        ("(el) => { el.src = '/a.png'; }", ".src="),
        ("() => document.createElement('script')", "createElement("),
        ("() => { location.href = '/x'; }", "location.href="),
        ("() => location.assign('/x')", "location.assign"),
        ("() => location.replace('/x')", "location.replace"),
        ("() => window.open('/x')", "window.open"),
    ],
)
def test_rule4_forbidden_words_in_page_scripts(script, label):
    source = f"DOM_SCRIPTS = {{'probe': {script!r}}}\nSTATE_SCRIPTS = {{}}\n"
    assert ("R4", label) in details(check_page_scripts(source, "platforms/x/page_scripts.py"))


@pytest.mark.parametrize(
    "script",
    [
        "(el) => el.src === location.href",
        "() => document.querySelector('img').currentSrc",
        "() => window.scrollY",
    ],
)
def test_rule4_allows_reading_without_requests(script):
    source = f"DOM_SCRIPTS = {{'probe': {script!r}}}\nSTATE_SCRIPTS = {{}}\n"
    assert check_page_scripts(source, "platforms/x/page_scripts.py") == []


def test_rule4_scans_runtime_script_literals_and_module_constants():
    source = '''
PROBE = "() => fetch('/x')"
CLEAN = "() => window.innerWidth"


async def run(page, context, dynamic):
    await page.evaluate(PROBE)
    await page.evaluate("() => new XMLHttpRequest()")
    await context.add_init_script(script="window.open('/x')")
    await page.evaluate(CLEAN)
    await page.evaluate(dynamic)
'''
    assert sorted(details(check_runtime_scripts(source, "runtime/x.py"))) == [
        ("R4", "XMLHttpRequest"), ("R4", "fetch("), ("R4", "window.open"),
    ]
    assert len(runtime_scripts(source)) == 4


def test_rule5_dom_scripts_must_not_read_page_state():
    source = "DOM_SCRIPTS = {'probe': '() => window.__INITIAL_STATE__'}\nSTATE_SCRIPTS = {}\n"
    assert ("R5", "dom:probe:__INITIAL_STATE__") in details(
        check_page_scripts(source, "platforms/x/page_scripts.py")
    )
    missing = "SCRIPTS = {}\n"
    assert details(check_page_scripts(missing, "platforms/x/page_scripts.py")) == [
        ("R5", "missing DOM_SCRIPTS/STATE_SCRIPTS registry")
    ]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("await page.evaluate(page_scripts.STATE_SCRIPTS['creator_page_data'])", "STATE_SCRIPTS"),
        ("name = 'creator_page_data'", "state:creator_page_data"),
        ("await channel.read_state(page, 'other', stage='x', subject_id=None, trigger=None)\n"
         "    script = 'creator_page_data'", "state:creator_page_data"),
    ],
)
def test_rule5_state_scripts_only_through_read_state(line, expected):
    source = f"async def run(page, channel, page_scripts):\n    {line}\n"
    assert ("R5", expected) in details(
        check_state_references(source, "platforms/x/overlay.py", frozenset({"creator_page_data"}))
    )


def test_rule5_allows_read_state_and_channel_registration():
    source = '''
async def run(page, channel, page_scripts, context, ledger):
    channel = PageCaptureChannel(context, platform="x", rules=(), ledger=ledger,
                                 state_scripts=page_scripts.STATE_SCRIPTS)
    await channel.read_state(page, "creator_page_data", None, stage="creator_profile",
                             subject_id="u", trigger=None)
    await channel.read_state(page, script_name="creator_page_data", stage="x", subject_id=None, trigger=None)
'''
    assert check_state_references(source, "platforms/x/session.py", frozenset({"creator_page_data"})) == []


def test_whitelist_must_match_exact_hits(monkeypatch, tmp_path):
    package = tmp_path / "src/trippostcollect"
    platform_dir = package / "platforms/demo"
    platform_dir.mkdir(parents=True)
    (platform_dir / "page_scripts.py").write_text("DOM_SCRIPTS = {}\nSTATE_SCRIPTS = {}\n")
    (platform_dir / "core.py").write_text("async def run(page):\n    return await page.content()\n")
    (package / "runtime").mkdir()
    (package / "runtime/clean.py").write_text("async def run(page):\n    return await page.evaluate('() => 1')\n")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", package)
    # 自测只检查合成平台：真实受检平台的目录不在临时包内。
    monkeypatch.setattr(sys.modules[__name__], "GUARDED_PLATFORMS", {"demo": "platforms/demo"})
    monkeypatch.setitem(RUNTIME_SCRIPT_MODULES, "demo", ("runtime/clean.py",))
    monkeypatch.setitem(WHITELISTS, "demo", frozenset())
    with pytest.raises(AssertionError):
        test_guarded_platforms_have_no_fetch_bypass()
    monkeypatch.setitem(WHITELISTS, "demo", frozenset({("platforms/demo/core.py", "R2", "content()")}))
    test_guarded_platforms_have_no_fetch_bypass()
    monkeypatch.setitem(
        WHITELISTS, "demo",
        frozenset({("platforms/demo/core.py", "R2", "content()"), ("platforms/demo/core.py", "R3", "evaluate")}),
    )
    with pytest.raises(AssertionError, match="白名单"):
        test_guarded_platforms_have_no_fetch_bypass()
