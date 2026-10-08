"""以迁移前冻结源码验证辅助入口的调用顺序、文件字节与判定。"""

from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime
import importlib
import json
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qsl

import pytest

from trippostcollect.application import inputs, page_evidence, repair, warmup
from trippostcollect.core import paths as core_paths
from trippostcollect.runtime import cookies, page_readiness

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
FIXTURES = ROOT / "tests/fixtures/adapter_t10"
ROWS = [row for row in json.loads((ROOT / "docs/adapter-ledger/symbols.json").read_text())["rows"] if row["card"] == "T10"]


def frozen_source(name):
    return (FIXTURES / f"{name}.py.txt").read_text()


def old_module(name, namespace=None):
    module = ModuleType(f"frozen_{name}")
    module.__dict__.update(namespace or {})
    source = frozen_source(name)
    if "from __future__ import annotations" not in source:
        source = "from __future__ import annotations\n" + source
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def function(source, name):
    return next(node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def t14_snapshot_writer(source):
    """T14 授权差异：快照路径由调用方经 platform_cookie_snapshot_path 给出，写前确保同级父目录存在。"""
    source = source.replace(
        "def write_cookie_snapshot(\n    platform_key: str,\n    profile_dir: Path,\n",
        "def write_cookie_snapshot(\n    platform_key: str,\n    snapshot_path: Path,\n",
    )
    return source.replace("    snapshot_path = profile_dir / COOKIE_SNAPSHOT_FILENAME\n", "    ensure_parent(snapshot_path)\n")


def t14_warmup_one(source):
    """T14 授权差异：建 profile 前做迁移失败关闭检查；快照写出不再传 profile 目录。"""
    source = source.replace(
        "    platform = PLATFORMS[platform_key]\n    profile_dir = profile_dir_for(platform_key)\n",
        "    platform = PLATFORMS[platform_key]\n    paths.require_platform_session_migrated(platform_key)\n"
        "    profile_dir = profile_dir_for(platform_key)\n",
    )
    return re.sub(r"(write_cookie_snapshot\(\n\s+platform_key,\n)\s+profile_dir,\n", r"\1", source)


def t14_without_fork_check(node):
    """T14 授权差异：入口不再要求 fork 目录存在。"""
    for child in ast.walk(node):
        body = getattr(child, "body", None)
        if isinstance(body, list):
            child.body = [statement for statement in body
                          if not (isinstance(statement, ast.If) and "MEDIACRAWLER_DIR" in ast.unparse(statement.test))]
    return node


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{Path(r['file']).stem}.{r['qualname']}")
def test_migrated_ast(row):
    name = row["qualname"]
    source = frozen_source(Path(row["file"]).stem)
    target_name = name
    target = ROOT / "src/trippostcollect" / row["target"]
    if row["file"] == "scripts/mediacrawler_login_warmup.py":
        source = source.replace("ALIASES.get(value)", "MEDIACRAWLER_ALIASES.get(value)")
        if name == "required_cookie_names":
            # T03 已合并，HEAD 中原入口直接重导出唯一实现。
            tree = ast.parse(source)
            assert any(isinstance(n, ast.ImportFrom) and n.module == "trippostcollect.runtime.cookies" and any(a.name == name for a in n.names) for n in tree.body)
            source = frozen_source("required_cookie_names")
        elif name in {"parse_args", "utc_stamp", "main_async", "main"}:
            target_name = "media_" + name
            for old in ("parse_args", "utc_stamp", "main_async", "main"):
                import re
                source = re.sub(r"(?<!\.)\b" + old + r"\b", "media_" + old, source)
            name = target_name
        elif name == "write_cookie_snapshot":
            source = source.replace("    source: str,\n", "    source: str,\n    label: str,\n    urls: list[str],\n")
            source = source.replace('PLATFORMS[platform_key]["label"]', "label").replace('PLATFORMS[platform_key]["urls"]', "urls")
            source = t14_snapshot_writer(source)
        elif name == "warmup_one":
            source = t14_warmup_one(source)
    if row["disposition"] == "薄":
        target = ROOT / "src/trippostcollect/application/warmup.py"
        if row["file"] == "scripts/login_warmup.py":
            target_name = "login_main"
        entry = function((ROOT / row["file"]).read_text(), "main")
        assert len(entry.body) == 1 and isinstance(entry.body[0], ast.Return)
    old = t14_without_fork_check(function(source, name))
    old.name = target_name
    new = function(target.read_text(), target_name)
    if target_name == "repair_runtime_stop_reason":
        new.args.kwonlyargs.clear()
        new.args.kw_defaults.clear()
    assert ast.dump(old) == ast.dump(new)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 30, 12, 0, 0, tzinfo=tz)


class FakePlaywright:
    """只记录调用；页面、Cookie、localStorage 与重开状态全部由替身提供。"""

    def __init__(self, scenario):
        self.trace = []
        self.scenario = scenario
        self.launches = 0
        self.chromium = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def launch_persistent_context(self, **kwargs):
        self.trace.append(("launch", kwargs))
        self.launches += 1
        return FakeContext(self, self.launches)


class FakeContext:
    def __init__(self, owner, index):
        self.owner, self.index = owner, index
        self.authenticated = owner.scenario != "manual" or index > 1
        self.desktop_reads = 0
        self.pages = [FakePage(self, 0)]

    async def new_page(self):
        self.owner.trace.append(("new_page", self.index))
        page = FakePage(self, len(self.pages))
        self.pages.append(page)
        return page

    async def cookies(self, urls=None):
        self.owner.trace.append(("cookies", self.index, urls))
        values = {"z_c0": "z", "d_c0": "d", "SUB": "s", "MLOGIN": "1", "SESSDATA": "b", "LOGIN_STATUS": "1"}
        if urls and "https://passport.weibo.com" in urls:
            self.desktop_reads += 1
            values["SSOLoginState"] = "signed" if self.desktop_reads > 1 else ""
        return [{"name": k, "value": v, "domain": ".example.test"} for k, v in values.items()]

    async def close(self):
        self.owner.trace.append(("close", self.index))


class FakePage:
    def __init__(self, context, index):
        self.context, self.index = context, index
        self.url = "about:blank"

    def record(self, *args):
        self.context.owner.trace.append((self.context.index, self.index, *args))

    async def goto(self, url, **kwargs):
        self.url = url
        self.record("goto", url, kwargs)

    async def reload(self, **kwargs):
        self.record("reload", kwargs)
        self.context.authenticated = True

    async def bring_to_front(self):
        self.record("front")

    async def wait_for_timeout(self, milliseconds):
        self.record("wait", milliseconds)
        if milliseconds == 60_000:
            raise KeyboardInterrupt

    async def evaluate(self, script):
        self.record("evaluate", script)
        if "localStorage" in script:
            return {"HasUserLogin": "1"}
        ok = self.context.authenticated
        if self.context.owner.scenario == "reopen_failed" and self.context.index > 1:
            ok = False
        return {"ok": ok, "login": ok, "uid": "123" if ok else None}


def files_under(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in root.rglob("*") if p.is_file()}


def configure(monkeypatch, module, fake, root):
    ticks = iter(range(10000))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks) / 100))
    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setattr(module, "profile_dir_for", lambda key: root / "profiles" / key)
    if hasattr(module, "MEDIACRAWLER_DIR"):
        monkeypatch.setattr(module, "MEDIACRAWLER_DIR", root)
    else:
        # T14：新实现不再检查 fork 目录，快照经 cookie_snapshot_path 写出；对照时落在与旧实现相同的文件。
        monkeypatch.setattr(module, "cookie_snapshot_path", lambda key: root / "profiles" / key / core_paths.COOKIE_SNAPSHOT_FILENAME)
        monkeypatch.setattr(core_paths, "LEGACY_FORK_PROFILE_ROOT", root.parent / "no-legacy-fork-profiles")
    monkeypatch.setattr(module, "discover_cdp_browser_path", lambda: None)
    monkeypatch.setattr(module, "browser_runtime_args", lambda: [])
    monkeypatch.setattr(module, "browser_launch_environment", lambda: {})
    monkeypatch.setattr(module, "async_playwright", lambda: fake)
    monkeypatch.setattr(cookies, "datetime", FixedDatetime)


@pytest.mark.parametrize("entry", ["one", "media", "unified"])
@pytest.mark.parametrize("scenario", ["valid", "manual", "reopen_failed", "skip", "keep"])
def test_warmup_trace_and_files(monkeypatch, tmp_path, entry, scenario):
    old = old_module("mediacrawler_login_warmup")
    monkeypatch.setitem(sys.modules, "mediacrawler_login_warmup", old)
    unified = old_module("login_warmup")
    results = []
    root = tmp_path / "run"
    for module in (old, warmup):
        root.mkdir()
        fake = FakePlaywright(scenario)
        configure(monkeypatch, module, fake, root)
        args = argparse.Namespace(timeout_seconds=1, output_dir=str(root), browser_path=None,
                                  no_close_on_success=scenario == "keep", skip_reopen_verify=scenario == "skip")
        if entry == "one":
            result = asyncio.run(module.warmup_one(fake, "weibo", root, args))
        elif entry == "media":
            argv = ["mediacrawler_login_warmup.py", "--platforms", "weibo", "--output-dir", str(root), "--timeout-seconds", "1"]
            if scenario in {"keep", "skip"}:
                argv.append("--no-close-on-success" if scenario == "keep" else "--skip-reopen-verify")
            monkeypatch.setattr(sys, "argv", argv)
            result = module.main() if module is old else module.media_main()
        else:
            runner = unified if module is old else module
            monkeypatch.setattr(runner, "datetime", FixedDatetime)
            if hasattr(runner, "MEDIACRAWLER_DIR"):
                monkeypatch.setattr(runner, "MEDIACRAWLER_DIR", root)
            monkeypatch.setattr(runner, "async_playwright", lambda: fake)
            monkeypatch.setattr(sys, "argv", ["login_warmup.py", "--targets", "all", "--output-dir", str(root), "--timeout-seconds", "1"])
            result = runner.main() if module is old else runner.login_main()
        results.append((result, fake.trace, files_under(root)))
        shutil.rmtree(root)
    assert results[0] == results[1]
    snapshots = [value for path, value in results[1][2].items() if path.endswith("trippostcollect_cookie_snapshot.json")]
    if scenario != "reopen_failed":
        assert snapshots
    assert all(mode == 0o600 for _, mode in snapshots)
    if entry == "unified":
        assert any(path.endswith("summary.md") for path in results[1][2])
    if scenario == "manual" and entry != "unified":
        assert any("front" in event for event in results[1][1])
        assert any("reload" in event for event in results[1][1])


def repair_baseline():
    crawl = importlib.import_module("mediacrawler_crawl")
    return old_module("mediacrawler_crawl", {**vars(crawl), "parse_qsl": parse_qsl})


def outcome(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (SystemExit, ValueError, TypeError) as exc:
        return type(exc).__name__, str(exc)


@pytest.mark.parametrize("name,payload", [
    ("load_zhihu_detail_urls", '["https://www.zhihu.com/question/1/answer/2?token=x#frag"]'),
    ("load_xhs_detail_urls", '["https://www.xiaohongshu.com/explore/a?xsec_token=secret&xsec_source=pc#frag"]'),
    ("load_xhs_repair_target_ids", '["a", "a", " b "]'),
    ("load_xhs_repair_report", '{"successful_ids":["a"]}'),
    ("load_post_repair_targets", '[{"platform_post_id":"123","detail_target":"123","keyword":"青岛"}]'),
])
@pytest.mark.parametrize("state", ["valid", "missing", "broken", "wrong_type", "empty"])
def test_repair_file_inputs(tmp_path, name, payload, state):
    path = tmp_path / "input.json"
    if state != "missing":
        path.write_text({"valid": payload, "broken": "{", "wrong_type": '"text"', "empty": "[]"}[state])
    old = repair_baseline()
    new = inputs if hasattr(inputs, name) else repair
    args = (path, "weibo") if name == "load_post_repair_targets" else (path,)
    assert outcome(getattr(old, name), *args) == outcome(getattr(new, name), *args)


@pytest.mark.parametrize("blocked,child_ok,valid,images,behavior", [
    (False, True, 0, False, False), (True, True, 1, True, True),
    (False, False, 1, True, True), (False, False, 0, True, True),
    (False, False, 1, False, True), (False, False, 1, True, False),
])
def test_repair_partial_execution(blocked, child_ok, valid, images, behavior):
    old = repair_baseline()
    kwargs = dict(repair_mode=True, runtime_blocked=blocked, child_execution_ok=child_ok,
                  validation={"valid_total_count": valid}, image_materialization={"complete": images},
                  behavior_validation={"ok": behavior})
    assert old.repair_partial_child_execution_allowed(**kwargs) == repair.repair_partial_child_execution_allowed(**kwargs)


@pytest.mark.parametrize("records", [[], [{"platform": "weibo", "run": {"returncode": 0}, "failure_classification": {"failure_type": "success"}}],
                                         [{"platform": "weibo", "run": {"returncode": 1}, "failure_classification": {"failure_type": "login_required"}}]])
def test_repair_completion_and_evidence(records):
    old = repair_baseline()
    assert old.repair_candidate_execution_completed(records, ["weibo"]) == repair.repair_candidate_execution_completed(records, ["weibo"])
    assert old.repair_runtime_stop_reason(records, ["weibo"]) == repair.repair_runtime_stop_reason(records, ["weibo"], latest_runtime_blocker=old.latest_runtime_blocker)
    assert old.xhs_repair_pagination_evidence(records, target_count=2) == repair.xhs_repair_pagination_evidence(records, target_count=2)
    targets = [{"platform_post_id": "1"}, {"platform_post_id": "2"}]
    kwargs = dict(platform="weibo", successful_identities={"weibo:id:1"}, materialization_failures=[{"identity": "weibo:id:2", "error_code": "failed"}])
    assert old.post_repair_pagination_evidence(targets, **kwargs) == repair.post_repair_pagination_evidence(targets, **kwargs)


def test_repair_fallback_sqlite(tmp_path):
    path = tmp_path / "fallback.sqlite"
    old = repair_baseline()
    assert old.load_post_repair_fallbacks(path, "weibo", {"1"}) == repair.load_post_repair_fallbacks(path, "weibo", {"1"}) == {}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE web_posts(platform_key, platform_post_id, keyword, raw_sample_json)")
        conn.execute("INSERT INTO web_posts VALUES (?, ?, ?, ?)", ("weibo", "1", "青岛", '{"liked_count": 0}'))
    result = repair.load_post_repair_fallbacks(path, "weibo", {"1", "2"})
    assert result == old.load_post_repair_fallbacks(path, "weibo", {"1", "2"})
    assert result == {"1": {"keyword": "青岛", "liked_count": 0}}


@pytest.mark.parametrize("navigation_error", [None, "timeout", "error"])
@pytest.mark.parametrize("ready", [True, False])
def test_page_readiness_trace(monkeypatch, navigation_error, ready):
    old = old_module("ctf_browser_resilience")

    class Page:
        url = "https://example.test/"

        def __init__(self):
            self.trace = []
            self.reads = 0
            self.clock = 0

        async def goto(self, url, **kwargs):
            self.trace.append(("goto", url, kwargs))
            if navigation_error:
                raise (old.PlaywrightTimeoutError if navigation_error == "timeout" else RuntimeError)("navigation")

        async def evaluate(self, script):
            self.trace.append(("evaluate", script))
            self.reads += 1
            return {"ready": ready and self.reads > 1, "body_text_length": 300 if self.reads > 1 else 0, "url": self.url}

        async def wait_for_timeout(self, value):
            self.trace.append(("wait", value))
            self.clock += value / 1000

    results = []
    for module in (old, page_readiness):
        page = Page()
        loop = SimpleNamespace(time=lambda: page.clock)
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(get_running_loop=lambda: loop))
        async def run():
            nav = await module.navigate_with_commit_and_readiness(page, page.url, commit_timeout_ms=10, readiness_timeout_ms=1)
            enrichment = await module.wait_for_content_enrichment(page, timeout_ms=1, poll_ms=1)
            return nav, enrichment
        results.append((asyncio.run(run()), page.trace))
    assert results[0] == results[1]


def test_page_evidence_cookie_cleanup(monkeypatch, tmp_path):
    old = old_module("ctf_browser_resilience")
    for module in (old, page_evidence):
        monkeypatch.setattr(module, "datetime", FixedDatetime)
    results = []
    for module in (old, page_evidence):
        db = tmp_path / "Default/Cookies"
        db.parent.mkdir(exist_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE cookies(host_key, name, path)")
            conn.executemany("INSERT INTO cookies VALUES (?, ?, ?)", [(".douyin.com", "a", "/"), (".example.test", "b", "/")])
        event = module.clean_douyin_profile_cookies(tmp_path)
        with sqlite3.connect(db) as conn:
            remaining = conn.execute("SELECT * FROM cookies").fetchall()
        db.unlink()
        trace = []
        class Context:
            async def cookies(self):
                trace.append("cookies")
                return [{"domain": ".douyin.com", "name": "a"}]
            async def clear_cookies(self):
                trace.append("clear")
        context_event = asyncio.run(module.clear_douyin_context_cookies(Context()))
        results.append((event, remaining, context_event, trace))
    assert results[0] == results[1]
    assert results[1][1] == [(".example.test", "b", "/")]


def test_cli_definition_selection():
    sys.path.insert(0, str(ROOT / "scripts/dev"))
    ledger = importlib.import_module("adapter_ledger")
    assert ledger.build_input_drift(ROOT) == {"cli_changed": {}, "env_added": [], "env_removed": []}


def test_missing_cli_definition_is_drift(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/dev"))
    ledger = importlib.import_module("adapter_ledger")
    entry = "scripts/login_warmup.py"
    selected = {entry: ("shared.py:chosen",)}
    source = 'def chosen():\n    parser.add_argument("--targets")\ndef other():\n    parser.add_argument("--platforms")\n'
    result = ledger.extract_inputs([], lambda name: source if name == "shared.py" else "", definition_sources=selected)
    assert result["cli"][entry] == [{"flags": ["--targets"]}]
    absent = ledger.extract_inputs([], lambda name: "", definition_sources=selected)
    assert absent["cli"][entry] == []


@pytest.mark.parametrize("target_exists,body,expected", [
    (True, "return run()", "moved"),
    (False, "return run()", "pending"),
    (True, "print('business'); return run()", "pending"),
])
def test_thin_entry_requires_existing_delegate(tmp_path, target_exists, body, expected):
    sys.path.insert(0, str(ROOT / "scripts/dev"))
    ledger = importlib.import_module("adapter_ledger")
    entry = tmp_path / "scripts/login.py"
    entry.parent.mkdir()
    entry.write_text(f"from trippostcollect.application.warmup import run\ndef main():\n    {body}\n")
    destination = tmp_path / "src/trippostcollect/application/warmup.py"
    destination.parent.mkdir(parents=True)
    destination.write_text("def run():\n    return 0\n" if target_exists else "")
    manifest = tmp_path / "docs/adapter-ledger/symbols.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"rows": [{"file": "scripts/login.py", "qualname": "main", "card": "T10", "disposition": "薄", "target": "scripts/login.py"}]}))
    assert ledger.build_progress(tmp_path)["rows"][0]["state"] == expected
