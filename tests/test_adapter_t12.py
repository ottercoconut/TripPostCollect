"""T12 验收：退出切片调用点切断、选站不装载 fork、D2 父发 env 收缩、CI 分组与测试台账对账。

基线 `tests/golden/t12_worker_env.json`、`tests/golden/t12_cli_help.json` 在 T12 实施前由 main
（ca85e82 / fork 3488cf2）生成：前者为 C 构造 worker 时的 extra_env，后者为各正式入口 `--help` 原文；
`tests/golden/t12_worker_config.json` 为五站装配函数实际收到的 worker 配置对象（当时为 fork config）的逐键值与类型。
构建与仓库外安装验收见 `test_adapter_t12_install.py`（installation lane）。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from support import legacy_expectations as expectations

from trippostcollect.application import collection as t11_collection
from trippostcollect.application import reporting as t11_reporting

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "scripts" / "dev"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

ledger = import_module("adapter_ledger")
GOLDEN = ROOT / "tests" / "golden"
FORK = ROOT / "tools" / "MediaCrawler"
PACKAGE = ROOT / "src" / "trippostcollect"
ENV_GOLDEN = json.loads((GOLDEN / "t12_worker_env.json").read_text(encoding="utf-8"))
HELP_GOLDEN = json.loads((GOLDEN / "t12_cli_help.json").read_text(encoding="utf-8"))
CONFIG_GOLDEN = json.loads((GOLDEN / "t12_worker_config.json").read_text(encoding="utf-8"))
T02_COMMANDS = json.loads((GOLDEN / "t02_worker_commands.json").read_text(encoding="utf-8"))
SELF = {"tests/test_adapter_t12.py", "tests/test_adapter_t12_install.py"}

# fork 顶层包；T12 起新 worker 选站后一个都不得装载（清单见 temp/t12-inventory.md 第 2 节）。
FORK_TOPS = frozenset({
    "api", "base", "cache", "cmd_arg", "config", "constant", "database", "libs", "main",
    "media_platform", "model", "proxy", "recv_sms", "schema", "store", "tools", "var",
})
# 盘点实测：main 上选站后仍装载 config（星号导入七站配置与 db_config）与 tools（微博行为桥）。
T12_CUT_FORK_TOPS = frozenset({"config", "tools"})
# 留到 T14 的 fork 顶层包：无。旧桥 E→fork main.py 不经过新入口，不在此列。
T14_ALLOWED_FORK_TOPS: frozenset[str] = frozenset()

# 旧桥：T14 才删除，T12 只放行，不扫描其退出引用（逐项理由见盘点报告第 1.3 节）。
OLD_BRIDGE_SCRIPTS = frozenset({"scripts/mediacrawler_export_entrypoint.py"})
# 闸门与台账工具由 G 卡独占，其中的退出名是规则数据，不是调用点。
GOVERNANCE_DIRS = ("scripts/dev/",)

# D2 中标注“父发无消费者，T12 移除发出”的 env（规格 D2 约 469、485 行）。
REMOVED_ENV = frozenset({
    "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID",
    "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM",
    "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD",
    "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE",
    "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED",
    "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS",
})

# 根测试中允许保留的退出名引用：(文件, 名字) → 类别。T14 删旧桥时同批清理。
# A＝旧桥等价对照/旧桥出口用例；B＝负向守卫（断言不存在或不被调用）；
# D＝fork 旧评论/存储出口的隐私用例（待协调者决定 T12 删除或保留到 T14）。
TEST_ALLOWLIST: dict[tuple[str, str], str] = {}
for _file, _names, _kind in (
    ("tests/test_adapter_t05_bridge.py", ("CrawlerFactory", "create_crawler", "init_db"), "A"),
    ("tests/test_adapter_t06_bridge.py", ("CrawlerFactory", "create_crawler"), "A"),
    ("tests/test_adapter_t07.py", ("CrawlerFactory", "create_crawler"), "A"),
    ("tests/test_adapter_t09.py", ("CrawlerFactory", "create_crawler"), "A"),
    ("tests/test_residual_detail_fallbacks.py", ("create_crawler",), "A"),
    ("tests/test_shared_staging.py", ("store_comment", "store_creator"), "A"),
    ("tests/test_author_avatar_sanitization.py", ("write_to_csv", "write_single_item_to_json"), "A"),
    ("tests/platforms/douyin/test_douyin_store.py", ("DouyinStoreFactory", "create_store"), "A"),
    ("tests/platforms/xhs/test_xhs_media_policy.py", ("get_notice_video", "update_xhs_note_video"), "B"),
    ("tests/platforms/xhs/test_xhs_login_contract.py", ("CacheFactory",), "B"),
    ("tests/platforms/douyin/test_douyin_image_only.py", ("get_aweme_video", "update_dy_aweme_video"), "B"),
    ("tests/test_adapter_t06.py", ("get_aweme_video",), "B"),
    ("tests/test_adapter_ledger.py", ("ProxyRefreshMixin", "AbstractCrawler", "create_ip_pool"), "B"),
    ("tests/platforms/douyin/test_douyin_no_user_info.py", (
        "store_comment", "DouyinStoreFactory", "create_store", "update_dy_aweme_comment",
        "DouyinAweme", "DouyinAwemeComment",
    ), "D"),
    ("tests/platforms/weibo/test_weibo_no_user_info.py", (
        "store_comment", "store_creator", "WeibostoreFactory", "create_store", "update_weibo_note_comment",
        "WeiboNote", "WeiboNoteComment",
    ), "D"),
    ("tests/support/douyin.py", (
        "create_store", "DouyinStoreFactory", "update_dy_aweme_comment", "_extract_comment_image_list",
    ), "D"),
    ("tests/support/weibo_privacy.py", ("create_store", "update_weibo_note_comment", "WeibostoreFactory"), "D"),
):
    for _name in _names:
        TEST_ALLOWLIST[(_file, _name)] = _kind


# ---------- 通用扫描 ----------

def exit_names() -> frozenset[str]:
    """与台账 exit_references 同一口径：退出定义名减去保留定义名；协议性 dunder 名不算调用点。"""
    rows = ledger.load_symbols(ROOT)["rows"]
    exited = {row["qualname"].split(".")[-1] for row in rows if row["disposition"] == "退"}
    kept = {row["qualname"].split(".")[-1] for row in rows if row["disposition"] != "退"}
    names = frozenset(name for name in exited - kept if not (name.startswith("__") and name.endswith("__")))
    assert len(names) > 200, "退出名集合异常偏小，不能以空集冒充零引用"
    return names


def python_files(*relatives: str) -> list[Path]:
    files = []
    for relative in relatives:
        base = ROOT / relative
        candidates = [base] if base.is_file() else sorted(base.rglob("*.py"))
        files.extend(path for path in candidates if "__pycache__" not in path.parts)
    assert files, relatives
    return files


def references(path: Path, names: frozenset[str]):
    """静态名、属性、导入别名、关键字参数，以及作为 getattr/patch 目标的字符串常量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found, kind = node.name, "def"
        elif isinstance(node, ast.Name):
            found, kind = node.id, "name"
        elif isinstance(node, ast.Attribute):
            found, kind = node.attr, "attr"
        elif isinstance(node, ast.alias):
            found, kind = node.name.split(".")[-1], "import"
        elif isinstance(node, ast.keyword):
            found, kind = node.arg, "keyword"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found, kind = node.value, "string"
        else:
            continue
        if found in names:
            yield found, kind, getattr(node, "lineno", 0)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def retained_production_files() -> list[Path]:
    files = python_files("src/trippostcollect", "scripts")
    return [
        path for path in files
        if relative(path) not in OLD_BRIDGE_SCRIPTS and not relative(path).startswith(GOVERNANCE_DIRS)
    ]


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


# ---------- 1. 退出符号在保留代码中的引用为零 ----------

def test_retained_package_and_scripts_have_no_exit_symbol_references() -> None:
    names = exit_names()
    hits = [
        f"{relative(path)}:{line} {kind} {name}"
        for path in retained_production_files()
        for name, kind, line in references(path, names)
    ]
    assert hits == []


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


def test_root_tests_reference_exit_symbols_only_through_registered_exceptions() -> None:
    names = exit_names()
    unexpected = []
    for path in python_files("tests"):
        if relative(path) in SELF:
            continue
        for name, kind, line in references(path, names):
            if (relative(path), name) not in TEST_ALLOWLIST:
                unexpected.append(f"{relative(path)}:{line} {kind} {name}")
    assert unexpected == []


def test_old_bridge_allowlist_is_exactly_the_t14_bridge() -> None:
    # 旧桥只有 E 一个根侧文件；fork 目录整体由 T14 删除，不能借放行名单扩大到其他脚本。
    assert OLD_BRIDGE_SCRIPTS == {"scripts/mediacrawler_export_entrypoint.py"}
    assert {kind for kind in TEST_ALLOWLIST.values()} <= {"A", "B", "D"}


@expectations.legacy_only
def test_fork_bridge_is_kept_only_for_old_bridge_until_t14() -> None:
    # 决策 5：模块文件保留到 T14，仅供旧桥与对照测试；docstring 写明用途与删除卡。
    module = ast.parse((PACKAGE / "platforms" / "_fork_bridge.py").read_text(encoding="utf-8"))
    docstring = ast.get_docstring(module) or ""
    assert "旧桥" in docstring and "T14" in docstring


@expectations.legacy_only
def test_old_bridge_export_hook_still_wraps_the_same_three_writer_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    # 决策 6：方法名改由旧桥 E 传入，E 包裹的方法集合与净化行为不变。
    import asyncio
    import types

    entrypoint = import_module("mediacrawler_export_entrypoint")
    assert tuple(entrypoint.EXPORT_METHODS) == ("write_to_csv", "write_to_jsonl", "write_single_item_to_json")
    calls = []

    class FakeWriter:
        async def write_to_csv(self, item, item_type):
            calls.append(("csv", dict(item)))

        async def write_to_jsonl(self, item, item_type):
            calls.append(("jsonl", dict(item)))

        async def write_single_item_to_json(self, item, item_type):
            calls.append(("json", dict(item)))

    fake_tools = types.ModuleType("tools")
    fake_tools.__path__ = []
    fake_writer = types.ModuleType("tools.async_file_writer")
    fake_writer.AsyncFileWriter = FakeWriter
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.async_file_writer", fake_writer)
    # E 会把 fork 目录插入 sys.path；用例结束后恢复，避免污染同进程其他用例。
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_STRIP_AUTHOR_AVATARS", "1")
    monkeypatch.delenv("TRIPPOSTCOLLECT_XHS_BATCH_CHECKPOINT_ENABLED", raising=False)
    entrypoint.install_export_hook()
    item = {"avatar_url": "https://example.invalid/a.jpg", "title": "青岛"}
    for method in entrypoint.EXPORT_METHODS:
        asyncio.run(getattr(FakeWriter(), method)(dict(item), "contents"))
    assert calls == [("csv", {"title": "青岛"}), ("jsonl", {"title": "青岛"}), ("json", {"title": "青岛"})]


def test_source_checkout_keeps_repository_root_without_explicit_work_root(tmp_path: Path) -> None:
    # 决策 9 的源码 checkout 一侧：未设 TRIPPOST_PROJECT_ROOT 时仍以仓库为根（安装态一侧见 install 用例）。
    environment = _child_environment({}, tmp_path)
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


# ---------- 2. 五站选站不装载 fork 顶层包 ----------

def _child_environment(extra_env: dict[str, str], tmp: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("TRIPPOSTCOLLECT_") and key not in {"PYTHONPATH", "TRIPPOST_PROJECT_ROOT"}}
    environment.update({
        key: value.replace("<TMP>", str(tmp)).replace("<ROOT>", str(ROOT)) for key, value in extra_env.items()
    })
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


SELECTION_PROBE = r'''
import json, sys
fork, scripts, argv, code = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]

def fork_modules():
    found = {}
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None) or ""
        search = [str(item) for item in (getattr(module, "__path__", None) or [])]
        if location.startswith(fork) or any(item.startswith(fork) for item in search):
            found.setdefault(name.split(".")[0], []).append(name)
    return {key: sorted(value) for key, value in sorted(found.items())}

try:
    from trippostcollect.platforms import _fork_bridge
except ImportError:  # T14-C 删除过渡装载点后，新入口自然无从调用
    _fork_bridge = None

def _forbidden_install():
    raise RuntimeError("新入口路径调用了过渡装载 _fork_bridge.install")

if _fork_bridge is not None:
    _fork_bridge.install = _forbidden_install
from trippostcollect.platforms.entry import configure, install_hooks, load_crawler
stages = {}
configure(argv); stages["configure"] = fork_modules()
install_hooks(); stages["install_hooks"] = fork_modules()
crawler_class = load_crawler(code); stages["load_crawler"] = fork_modules()
crawler = crawler_class(); stages["instantiate"] = fork_modules()
paths = [item for item in sys.path if item.startswith((fork, scripts))]
print(json.dumps({"stages": stages, "paths": paths, "class": crawler_class.__name__}))
'''


@pytest.mark.parametrize("scenario", sorted(T02_COMMANDS))
def test_worker_selection_loads_no_fork_top_level_package(scenario: str, tmp_path: Path) -> None:
    golden = T02_COMMANDS[scenario]
    argv = [part.replace("<TMP>", str(tmp_path)) for part in golden["cmd"][4:]]
    code = argv[argv.index("--platform") + 1]
    result = subprocess.run(
        [sys.executable, "-P", "-c", SELECTION_PROBE, str(FORK), str(ROOT / "scripts"), json.dumps(argv), code],
        cwd=tmp_path, env=_child_environment(golden["extra_env"], tmp_path),
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    loaded = set().union(*(set(stage) for stage in report["stages"].values()))
    assert loaded & T12_CUT_FORK_TOPS == set(), report["stages"]
    assert loaded <= T14_ALLOWED_FORK_TOPS, report["stages"]
    # 过渡装载点不再把 fork 与 scripts 插入新 worker 的 sys.path。
    assert report["paths"] == []


CONFIG_PROBE = r'''
import json, sys
argv, code, keys = json.loads(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
from trippostcollect.platforms import entry
seen = []
for name in ("weibo_dependencies", "douyin_dependencies", "_zhihu_dependencies", "xhs_dependencies"):
    original = getattr(entry, name)
    def wrapped(config, *args, _original=original, **kwargs):
        seen.append(config)
        return _original(config, *args, **kwargs)
    setattr(entry, name, wrapped)
entry.configure(argv)
entry.install_hooks()
entry.load_crawler(code)()
config = seen[0]
assert all(item is config for item in seen)
snapshot = {key: {"type": type(getattr(config, key)).__name__, "value": getattr(config, key)}
            for key in keys if hasattr(config, key)}
names = sorted(name for name in dir(config) if name.isupper())
print(json.dumps({"snapshot": snapshot, "names": names}, ensure_ascii=False))
'''
# 规格 C0/D1“不迁上游示例 ID”：这些键在未由父侧传入 --specified_id 时，fork 默认值是上游示例 ID。
# 根配置对象对它们取空列表；父侧传入时须与旧值逐项相同。其余键逐键值与类型相等（决策 4）。
UPSTREAM_EXAMPLE_ID_KEYS = {
    "dy": "DY_SPECIFIED_ID_LIST", "wb": "WEIBO_SPECIFIED_ID_LIST",
    "zhihu": "ZHIHU_SPECIFIED_ID_LIST", "xhs": "XHS_SPECIFIED_NOTE_URL_LIST",
}
# 上游 DB 凭据、缓存、代理供应商、词云与退出平台配置不得进入根配置对象。
FORBIDDEN_CONFIG_PREFIXES = (
    "MYSQL_", "REDIS_", "MONGODB_", "POSTGRES_", "SQLITE_", "CACHE_TYPE_", "BILI_", "KS_", "TIEBA_",
    "IP_PROXY_", "STATIC_PROXY_", "CUSTOM_WORDS", "STOP_WORDS_FILE", "FONT_PATH", "ENABLE_GET_WORDCLOUD",
)


def required_config_keys() -> list[str]:
    """五站装配实际读取的键：各站设置数据类字段、浏览器设置字段与直接读取的四个键。"""
    from dataclasses import fields

    from trippostcollect.application.contracts import DouyinSettings, XhsSettings, ZhihuSettings
    from trippostcollect.platforms.weibo.models import WeiboConfig
    from trippostcollect.runtime.browser import CDPBrowserSettings

    keys = {"DISABLE_SSL_VERIFY", "SAVE_DATA_PATH", "COOKIES", "LOGIN_TYPE"}
    for cls in (DouyinSettings, ZhihuSettings, XhsSettings, WeiboConfig, CDPBrowserSettings):
        keys.update(field.name for field in fields(cls))
    return sorted(keys)


@pytest.mark.parametrize("scenario", sorted(T02_COMMANDS))
def test_worker_config_matches_fork_defaults_key_by_key(scenario: str, tmp_path: Path) -> None:
    golden_command = T02_COMMANDS[scenario]
    expected = CONFIG_GOLDEN[scenario]
    keys = required_config_keys()
    assert set(keys) <= set(expected), sorted(set(keys) - set(expected))
    argv = [part.replace("<TMP>", str(tmp_path)) for part in golden_command["cmd"][4:]]
    code = argv[argv.index("--platform") + 1]
    result = subprocess.run(
        [sys.executable, "-P", "-c", CONFIG_PROBE, json.dumps(argv), code, json.dumps(keys)],
        cwd=tmp_path, env=_child_environment(golden_command["extra_env"], tmp_path),
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1].replace(str(tmp_path), "<TMP>").replace(str(ROOT), "<ROOT>"))
    assert sorted(report["snapshot"]) == keys
    specified = "--specified_id" in argv
    for key in keys:
        actual, old = report["snapshot"][key], expected[key]
        if key in UPSTREAM_EXAMPLE_ID_KEYS.values() and not (specified and UPSTREAM_EXAMPLE_ID_KEYS[code] == key):
            assert actual == {"type": "list", "value": []}, key
        else:
            assert actual == old, key
    leaked = [name for name in report["names"] if name.startswith(FORBIDDEN_CONFIG_PREFIXES)]
    assert leaked == []


def test_bilibili_runs_in_process_without_fork_and_is_not_a_worker(tmp_path: Path) -> None:
    probe = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "import mediacrawler_crawl, crawl_runner\n"
        "from trippostcollect.platforms.bilibili import core, client, parser, login, signer\n"
        "from trippostcollect.platforms.entry import load_crawler\n"
        "try:\n    load_crawler('bili')\nexcept ValueError:\n    pass\nelse:\n    raise SystemExit('bili')\n"
        "fork = sys.argv[1]\n"
        "print(json.dumps(sorted({n.split('.')[0] for n, m in list(sys.modules.items())\n"
        "                         if (getattr(m, '__file__', None) or '').startswith(fork)})))\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", probe, str(FORK), str(ROOT / "scripts")],
        cwd=tmp_path, env=_child_environment({}, tmp_path), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_prerequisites_do_not_require_fork_source_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 规格 C1 末行：core/resources 替代 fork 文件存在检查（T01/T12）；profile 位置仍由 core.paths 定义至 T14。
    crawl = import_module("mediacrawler_crawl")
    calls = []
    monkeypatch.setattr(crawl, "verify_package_resources", lambda: calls.append(True))
    if hasattr(crawl, "MEDIACRAWLER_DIR"):
        monkeypatch.setattr(crawl, "MEDIACRAWLER_DIR", tmp_path / "missing-fork")
    crawl.ensure_prerequisites()
    assert calls == [True]


# ---------- 3. D2 父发无消费者 env ----------

def _capture_extra_env(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> dict[str, dict[str, str]]:
    crawl = import_module("mediacrawler_crawl")
    captured: dict[str, dict[str, str]] = {}

    def fake_run_command(cmd, cwd, timeout, log_dir, **kwargs):
        captured["extra_env"] = dict(kwargs.get("extra_env") or {})
        return {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": ""}

    monkeypatch.delenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", raising=False)
    monkeypatch.setattr(t11_collection, "run_command", fake_run_command)
    monkeypatch.setattr(t11_collection, "discover_cdp_browser_path", lambda: "/fake/chrome")
    monkeypatch.setattr(
        t11_collection, "export_profile_cookies",
        lambda *_: {"cookie_header": "d_c0=x; z_c0=y", "cookie_names": ["d_c0", "z_c0"], "source": "fake"},
    )
    monkeypatch.setattr(t11_collection, "public_cookie_export", lambda _export: {"source": "fake"})
    monkeypatch.setattr(
        t11_reporting, "summarize_output",
        lambda *_: {"parse_errors": 0, "content_records": 0, "non_video_content_records": 0, "video_like_records": 0},
    )
    monkeypatch.setattr(t11_collection, "load_behavior_evidence", lambda *_: {"status": "completed"})
    monkeypatch.setattr(t11_collection, "behavior_evidence_valid", lambda *_: True)
    result = {}
    for name, golden in ENV_GOLDEN.items():
        if name == "_meta":
            continue
        values = {key: value for key, value in golden["args"].items() if key != "platform"}
        args = argparse.Namespace(**{
            key: (value.replace("<TMP>", str(tmp)) if isinstance(value, str) else value)
            for key, value in values.items()
        })
        captured.clear()
        crawl._run_platform_without_policy(golden["platform"], args, tmp / name)
        text = json.dumps(captured["extra_env"], ensure_ascii=False)
        result[name] = json.loads(text.replace(str(tmp), "<TMP>").replace(str(ROOT), "<ROOT>"))
    return result


def test_unconsumed_parent_env_is_no_longer_emitted_and_rest_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    baseline = {name: value["extra_env"] for name, value in ENV_GOLDEN.items() if name != "_meta"}
    # 基线确实覆盖了每个待删项，否则“不再发出”无从证明。
    assert REMOVED_ENV <= set().union(*(set(env) for env in baseline.values()))
    current = _capture_extra_env(monkeypatch, tmp_path)
    assert sorted(current) == sorted(baseline)
    for name, env in baseline.items():
        assert REMOVED_ENV.isdisjoint(current[name]), name
        expected = {key: value for key, value in env.items() if key not in REMOVED_ENV}
        assert current[name] == expected, name


def test_removed_env_names_have_no_producer_or_consumer() -> None:
    pattern = re.compile("|".join(sorted(REMOVED_ENV)))
    files = retained_production_files() + [ROOT / name for name in ledger.fork_python_files(ROOT)]
    hits = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


# ---------- 4. 正式入口 CLI 与 main 逐字相同 ----------

def _help_commands() -> dict[str, list[str]]:
    commands = {
        name: [sys.executable, str(ROOT / "scripts" / f"{name}.py"), "--help"]
        for name in HELP_GOLDEN if name not in {"_meta", "trippostcollect.platforms.entry"}
    }
    commands["trippostcollect.platforms.entry"] = [sys.executable, "-P", "-m", "trippostcollect.platforms.entry", "--help"]
    return commands


def test_formal_entry_help_is_identical_to_main(tmp_path: Path) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    environment = _child_environment({}, tmp_path)
    # 决策 15：显式固定影响 argparse 排版的终端尺寸与区域设置，不依赖宿主终端。
    environment.update(COLUMNS=str(HELP_GOLDEN["_meta"]["columns"]), LINES="24", LC_ALL="C.UTF-8",
                       TRIPPOST_PROJECT_ROOT=str(workroot))
    for name in ("PYTHON_COLORS", "FORCE_COLOR", "NO_COLOR", "TERM"):
        environment.pop(name, None)
    differences = []
    for name, command in _help_commands().items():
        result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
        actual = result.stdout.replace(str(workroot), "<ROOT>")
        if result.returncode or result.stderr or actual != HELP_GOLDEN[name]:
            differences.append(name)
    assert differences == []


# ---------- 5. 资源单一真源 ----------

def test_generated_resources_are_not_hand_copied_into_source_tree() -> None:
    resources = PACKAGE / "resources"
    assert not (resources / "sql").exists()
    assert not (resources / "contracts").exists()
    assert sorted(path.name for path in (ROOT / "db").glob("*.sql")) == [
        "crawl_scheduler.sql", "ctf_captures.sql", "source_platforms.sql", "web_posts.sql", "xhs_control.sql",
    ]
    assert (ROOT / "docs" / "formal-crawl-contract.md").is_file()


# ---------- 6. CI 分组、FORK 清单与测试台账 ----------

def _load_run_matrix():
    import importlib.util

    spec = importlib.util.spec_from_file_location("t12_run_matrix", ROOT / "scripts" / "ci" / "run_matrix.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _ledger_nodes():
    return json.loads((ROOT / "docs" / "adapter-ledger" / "tests.json").read_text(encoding="utf-8"))


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.update(f"{node.name}::{item.name}" for item in node.body
                         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def test_fork_offline_list_matches_ledger_nodes_still_in_fork() -> None:
    matrix = _load_run_matrix()
    offline = set(matrix.FORK_OFFLINE_TESTS)
    for name in offline:
        assert (FORK / name).is_file(), name
    still = [
        node for node in _ledger_nodes()["fork"]["nodes"]
        if node["source_file"].removeprefix("tools/MediaCrawler/") in offline
    ]
    for node in still:
        function = node["node_id"].split("::", 1)[1].split("[", 1)[0]
        assert function in _function_names(ROOT / node["source_file"]), node["node_id"]
        # 同一旧节点不能同时留在 fork 又迁入根目标文件。
        target = ROOT / node["target_file"]
        assert not target.exists() or function not in _function_names(target), node["node_id"]
    assert len(still) == matrix.FORK_EXPECTED_TESTS


ERRNO_PARAMS = {"51": "ENETUNREACH", "60": "ETIMEDOUT", "61": "ECONNREFUSED",
                "101": "ENETUNREACH", "110": "ETIMEDOUT", "111": "ECONNREFUSED"}
# 台账收集于 macOS：errno 参数值随平台变化，按符号名归一。
ERRNO_PARAMETRIZED = {"tests/test_run_lanes.py::test_seatbelt_does_not_accept_other_layers_errors"}
# 台账删除登记（决策 12，不手改 tests.json、不写入 testing.md）：
# T09（#41）随 creator 批采退出删除该 fork 节点，小红书迁入用例由 250 变为 249。T14 前不得再出现其他缺失。
DELETED_LEDGER_NODES = {"tests/test_xhs_core_access_error.py::test_creator_flow_stops_on_blocked_creator"}
# 决策 13：按台账 target_file 迁移，名称与断言不变；迁移后只能出现在目标文件。
MOVED_TO_TARGET = {
    "tests/test_run_ids.py::test_all_script_run_ids_include_microseconds":
        "tests/test_platform_package_resources.py::test_all_script_run_ids_include_microseconds",
}


def _normalize(node_id: str) -> str:
    base, bracket, rest = node_id.partition("[")
    if base in ERRNO_PARAMETRIZED and bracket:
        return f"{base}[{ERRNO_PARAMS.get(rest.rstrip(']'), rest.rstrip(']'))}]"
    return node_id


def test_ledger_nodes_reconcile_with_current_root_collection(tmp_path: Path) -> None:
    environment = _child_environment({}, tmp_path)
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / name) for name in ("src", "tests", "scripts"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "pytest_asyncio.plugin", "--strict-markers"],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    collected = {_normalize(line.strip()) for line in result.stdout.splitlines() if "::" in line}
    matrix = _load_run_matrix()
    offline = set(matrix.FORK_OFFLINE_TESTS)
    data = _ledger_nodes()
    missing = []
    for side in ("root", "fork"):
        for node in data[side]["nodes"]:
            name = node["node_id"].split("::", 1)[1]
            places = {_normalize(f"{node['target_file']}::{name}")}
            if side == "root":
                places.add(_normalize(node["node_id"]))
            in_root = bool(places & collected)
            in_fork = side == "fork" and node["source_file"].removeprefix("tools/MediaCrawler/") in offline
            if in_root == in_fork:
                missing.append(node["node_id"])
    assert set(missing) == DELETED_LEDGER_NODES
    for source, target in MOVED_TO_TARGET.items():
        assert source not in collected and target in collected, (source, target)
    assert len(data["root"]["nodes"]) + len(data["fork"]["nodes"]) - len(missing) == 1418


def test_testing_doc_matches_ci_lists() -> None:
    text = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
    matrix = _load_run_matrix()
    match = re.search(r"FORK_OFFLINE_TESTS` 明确列出的 (\d+) 文件、(\d+) 个", text)
    assert match, "testing.md 未登记 fork 离线清单计数"
    assert (int(match.group(1)), int(match.group(2))) == (len(matrix.FORK_OFFLINE_TESTS), matrix.FORK_EXPECTED_TESTS)
    # fork 静态导入 B站 旧描述随 run_matrix 同批更新。
    assert "静态导入 B站" not in text
