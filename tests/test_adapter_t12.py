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

# 旧桥 E 与过渡装载点：T12 只放行，T14-C 随 fork 删除；之后保留代码一律扫描，不再有放行文件。
DELETED_BRIDGE_FILES = ("scripts/mediacrawler_export_entrypoint.py", "src/trippostcollect/platforms/_fork_bridge.py")
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
# T14-C 删除：唯一消费者是 fork 旧 store 的作者原值开关，根实现不读取（见 ledger.AUTHORIZED_ENV_REMOVALS）。
T14C_REMOVED_ENV = frozenset({"TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL"})

# 根测试中允许保留的退出名引用：(文件, 名字) → 类别。T14-C 删旧桥时已清理旧桥对照（原 A 类）。
# B＝负向守卫（断言不存在或不被调用）；D＝沿用旧存储出口名的测试装配与隐私用例（名字指向根实现或
# 冻结旧 ORM 列契约，不加载 fork）。
TEST_ALLOWLIST: dict[tuple[str, str], str] = {}
for _file, _names, _kind in (
    ("tests/test_shared_staging.py", ("store_comment", "store_creator"), "B"),
    ("tests/platforms/xhs/test_xhs_media_policy.py", ("get_notice_video", "update_xhs_note_video"), "B"),
    ("tests/platforms/xhs/test_xhs_login_contract.py", ("CacheFactory",), "B"),
    ("tests/platforms/douyin/test_douyin_image_only.py", ("get_aweme_video", "update_dy_aweme_video"), "B"),
    ("tests/test_adapter_t06.py", ("get_aweme_video",), "B"),
    ("tests/test_adapter_ledger.py", ("ProxyRefreshMixin", "AbstractCrawler", "create_ip_pool"), "B"),
    ("tests/platforms/douyin/test_douyin_store.py", ("DouyinStoreFactory", "create_store"), "D"),
    ("tests/platforms/douyin/test_douyin_no_user_info.py", ("DouyinStoreFactory", "create_store", "DouyinAweme"), "D"),
    ("tests/platforms/weibo/test_weibo_no_user_info.py", ("WeibostoreFactory", "create_store", "WeiboNote"), "D"),
    ("tests/support/douyin.py", ("create_store", "DouyinStoreFactory"), "D"),
    ("tests/support/weibo_privacy.py", ("create_store", "WeibostoreFactory"), "D"),
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


def test_old_bridge_and_fork_no_longer_exist() -> None:
    # T14-C：旧桥 E 与过渡装载点已删除，保留代码不再有放行文件。fork 目录不在此断言：Mac 工作副本删除
    # gitlink 后旧目录可作为未跟踪备份留在磁盘（已由 .gitignore 忽略），以台账与 CI 副本为准。
    for name in DELETED_BRIDGE_FILES:
        assert not (ROOT / name).exists(), name
    assert {kind for kind in TEST_ALLOWLIST.values()} <= {"B", "D"}


def test_test_allowlist_has_no_stale_entries() -> None:
    # 放行项必须仍有对应引用；删除用例后同步收缩，不得留作扩大放行的空位。
    names = exit_names()
    used = {(relative(path), name) for path in python_files("tests") if relative(path) not in SELF
            for name, _, _ in references(path, names)}
    assert set(TEST_ALLOWLIST) - used == set()


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
    # T14-C 有意偏离：USER_DATA_DIR 只为 fork 浏览器桥按名构造而保留，随 fork 删除；钉住基线旧值后断言已移除。
    assert expected["USER_DATA_DIR"] == {"type": "str", "value": "%s_user_data_dir"}
    assert "USER_DATA_DIR" not in report["names"]


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
    # 规格 C1 末行：core/resources 替代 fork 文件存在检查（T01/T12）；fork 目录在 T14-C 删除。
    crawl = import_module("mediacrawler_crawl")
    calls = []
    monkeypatch.setattr(crawl, "verify_package_resources", lambda: calls.append(True))
    assert not hasattr(crawl, "MEDIACRAWLER_DIR")
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
    removed = REMOVED_ENV | T14C_REMOVED_ENV
    # 基线确实覆盖了每个待删项，否则“不再发出”无从证明。
    assert removed <= set().union(*(set(env) for env in baseline.values()))
    current = _capture_extra_env(monkeypatch, tmp_path)
    assert sorted(current) == sorted(baseline)
    for name, env in baseline.items():
        assert removed.isdisjoint(current[name]), name
        expected = {key: value for key, value in env.items() if key not in removed}
        assert current[name] == expected, name


def test_removed_env_names_have_no_producer_or_consumer() -> None:
    pattern = re.compile("|".join(sorted(REMOVED_ENV | T14C_REMOVED_ENV)))
    files = retained_production_files()
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


# ---------- 6. CI 分组与测试台账 ----------

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


ERRNO_PARAMS = {"51": "ENETUNREACH", "60": "ETIMEDOUT", "61": "ECONNREFUSED",
                "101": "ENETUNREACH", "110": "ETIMEDOUT", "111": "ECONNREFUSED"}
# 台账收集于 macOS：errno 参数值随平台变化，按符号名归一。
ERRNO_PARAMETRIZED = {"tests/test_run_lanes.py::test_seatbelt_does_not_accept_other_layers_errors"}
# 台账删除登记（决策 12，不手改 tests.json、不写入 testing.md）：
# T09（#41）随 creator 批采退出删除该 fork 节点，小红书迁入用例由 250 变为 249。T14 前不得再出现其他缺失。
DELETED_LEDGER_NODES = {"tests/test_xhs_core_access_error.py::test_creator_flow_stops_on_blocked_creator"}
# T14-C 随 fork、私有桥 E 与旧身份函数删除的 6 个台账节点（用户授权的删除批）：
# - E 的 export/批次 hook 自测（根侧由 worker 出口用例覆盖：test_worker_store_files_remove_avatar_before_serialization、
#   test_worker_checkpoint_exit_order_and_failures[worker-*]）；
# - 只断言冻结旧评论投影哈希/脱敏的两个 fork 隐私用例（评论为 T12 退出切片，anonymize_user_id/mask_nickname 已删除）。
T14C_DELETED_LEDGER_NODES = {
    "tests/test_author_avatar_sanitization.py::test_mediacrawler_export_hook_wraps_writer_before_persistence",
    "tests/test_xhs_batch_checkpoint.py::test_exporter_checkpoint_failure_keeps_precise_terminal_evidence"
    "[error0-xhs_batch_checkpoint_filenotfounderror]",
    "tests/test_xhs_batch_checkpoint.py::test_exporter_checkpoint_failure_keeps_precise_terminal_evidence"
    "[error1-xhs_batch_checkpoint_ack_timeout]",
    "tests/test_xhs_batch_checkpoint.py::test_exporter_hook_publishes_only_after_durable_event",
    "tests/test_douyin_no_user_info.py::test_douyin_comment_masks_user_info",
    "tests/test_weibo_no_user_info.py::test_weibo_comment_masks_user_info",
}
# #64 清理批随 fork 过渡代码与台账生成路径删除的 4 个台账节点：fork lane 的 PYTHONPATH/命令拼装、
# 符号账生成的未映射报告、产物比对 check_file、tests 收集子命令（生成与收集已删除，台账只做冻结自检）。
T64_DELETED_LEDGER_NODES = {
    "tests/test_run_lanes.py::test_fork_worker_paths_and_pytest_boundary",
    "tests/test_adapter_ledger.py::test_unmapped_definition_is_reported",
    "tests/test_adapter_ledger.py::test_check_mode_detects_stale_artifact",
    "tests/test_adapter_ledger.py::test_tests_collector_refuses_production_checkout",
}
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
    data = _ledger_nodes()
    missing = []
    for side in ("root", "fork"):
        for node in data[side]["nodes"]:
            name = node["node_id"].split("::", 1)[1]
            places = {_normalize(f"{node['target_file']}::{name}")}
            if side == "root":
                places.add(_normalize(node["node_id"]))
            # fork 离线 lane 已随 fork 删除：两侧台账节点都只能在根收集中找到。
            if not places & collected:
                missing.append(node["node_id"])
    assert set(missing) == DELETED_LEDGER_NODES | T14C_DELETED_LEDGER_NODES | T64_DELETED_LEDGER_NODES
    for source, target in MOVED_TO_TARGET.items():
        assert source not in collected and target in collected, (source, target)
    assert len(data["root"]["nodes"]) + len(data["fork"]["nodes"]) - len(missing) == 1418 - len(T14C_DELETED_LEDGER_NODES) - len(T64_DELETED_LEDGER_NODES)
