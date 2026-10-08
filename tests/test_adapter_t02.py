"""T02 验收：worker 改由根解释器运行选站入口，输入/配置/命令与旧桥等价，未选平台不装载。

基线 `tests/golden/t02_worker_*.json` 在 T02 实施前由旧实现生成：
commands 为 C 构造的 child 命令/cwd/env/超时；config 为原 typer `parse_cmd` 解析后写入的全部 config 键。
"""

from __future__ import annotations

from trippostcollect.application import collection as t11_collection
from trippostcollect.application import reporting as t11_reporting

import argparse
import json
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from support import legacy_expectations as expectations
from support.platform_sessions import child_redirect_source

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "scripts" / "dev"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

GOLDEN = ROOT / "tests" / "golden"
COMMANDS = json.loads((GOLDEN / "t02_worker_commands.json").read_text(encoding="utf-8"))
CONFIG = json.loads((GOLDEN / "t02_worker_config.json").read_text(encoding="utf-8"))
SCENARIOS = sorted(COMMANDS)
FORK = ROOT / "tools" / "MediaCrawler"
ENTRY_MODULE = "trippostcollect.platforms.entry"
# 退出切片依赖：根环境不安装，四站与入口导入链不得触及。
EXIT_SLICE_MODULES = ("redis", "sqlalchemy", "aiomysql", "motor", "jieba", "matplotlib", "wordcloud", "typer")
WORKER_PLATFORMS = {"wb": "weibo", "dy": "douyin", "zhihu": "zhihu", "xhs": "xhs"}
UNSELECTED_FORK_PLATFORMS = {"weibo", "douyin", "zhihu", "xhs", "bilibili", "kuaishou", "tieba"}


def _old_argv(name: str, tmp: Path) -> list[str]:
    # 旧命令为 ["uv", "run", "python", E.py, *argv]
    return [part.replace("<TMP>", str(tmp)) for part in COMMANDS[name]["cmd"][4:]]


def _child_environment() -> dict[str, str]:
    """只保留被测副本自己的 src，避免 lane 的 tests/scripts 路径或生产 checkout 掩盖入口装载缺陷。"""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _run_python(code: str, *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """在隔离子进程中运行，避免 fork 的 config/tools 顶层包污染当前 pytest 进程。"""
    blocker = (
        "import sys\n"
        f"_BLOCKED = {EXIT_SLICE_MODULES!r}\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in _BLOCKED:\n"
        "            raise ImportError('exit-slice module imported: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
    )
    environment = _child_environment()
    return subprocess.run(
        [sys.executable, "-P", "-c", blocker + code],
        cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _json_tail(result: subprocess.CompletedProcess[str]):
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------- C 构造的 child 命令 ----------

def _capture_new_commands(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> dict[str, dict]:
    crawl = import_module("mediacrawler_crawl")
    captured: dict[str, object] = {}

    def fake_run_command(cmd, cwd, timeout, log_dir, **kwargs):
        captured.update(
            cmd=list(cmd), cwd=str(cwd), timeout=timeout,
            extra_env=dict(kwargs.get("extra_env") or {}),
            startup_grace_seconds=kwargs.get("startup_grace_seconds"),
            network_diagnostics_path=(
                str(kwargs["network_diagnostics_path"]) if kwargs.get("network_diagnostics_path") else None
            ),
        )
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
    for name in SCENARIOS:
        golden = COMMANDS[name]
        args = argparse.Namespace(**{
            key: (value.replace("<TMP>", str(tmp)) if isinstance(value, str) else value)
            for key, value in golden["args"].items()
        })
        captured.clear()
        crawl._run_platform_without_policy(golden["platform"], args, tmp / name)
        text = json.dumps(captured, ensure_ascii=False)
        text = text.replace(str(tmp), "<TMP>").replace(str(ROOT), "<ROOT>")
        result[name] = json.loads(text)
    return result


T12_REMOVED_ENV = {
    "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID", "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM", "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD",
    "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE", "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED",
    "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS",
}


def test_child_command_switches_only_interpreter_entry_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    new = _capture_new_commands(monkeypatch, tmp_path)
    for name in SCENARIOS:
        old = COMMANDS[name]
        assert old["cmd"][:4] == ["uv", "run", "python", "<ROOT>/scripts/mediacrawler_export_entrypoint.py"]
        interpreter = sys.executable.replace(str(tmp_path), "<TMP>").replace(str(ROOT), "<ROOT>")
        expected_cmd = [interpreter, "-P", "-m", ENTRY_MODULE, *old["cmd"][4:]]
        assert new[name]["cmd"] == expected_cmd, name
        assert new[name]["cwd"] == "<ROOT>", name
        for key in ("timeout", "startup_grace_seconds", "network_diagnostics_path"):
            assert new[name][key] == old[key], (name, key)
        # T12：规格 D2 授权删除父发无消费者的 6 个 env，其余键值不变。
        expected_env = {key: value for key, value in old["extra_env"].items() if key not in T12_REMOVED_ENV}
        assert new[name]["extra_env"] == expected_env, name


def test_executor_source_no_longer_builds_uv_or_private_bridge_command() -> None:
    source = (ROOT / "scripts" / "mediacrawler_crawl.py").read_text(encoding="utf-8")
    assert "mediacrawler_export_entrypoint" not in source
    assert '"uv"' not in source
    assert "UV_CACHE_DIR" not in source


def test_child_environment_does_not_inject_uv_cache_or_pythonpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    process = import_module("trippostcollect.runtime.process")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    child = "import os, sys; sys.exit(int('UV_CACHE_DIR' in os.environ or 'PYTHONPATH' in os.environ))"
    result = process.run_command([sys.executable, "-c", child], tmp_path, 10, tmp_path / "logs")
    assert result["returncode"] == 0, result


def test_executor_reexports_moved_runtime_definitions() -> None:
    # C 仍以模块名暴露迁出的定义，调用方与既有 monkeypatch 目标不变
    crawl = import_module("mediacrawler_crawl")
    process = import_module("trippostcollect.runtime.process")
    helpers = import_module("trippostcollect.runtime.helpers")
    contracts = import_module("trippostcollect.application.contracts")
    for name in ("run_command", "skipped_command", "terminate_managed_process", "process_group_exists",
                 "runtime_watchdog_stop_detail", "append_runtime_watchdog_stop_event", "XhsParentNetworkPauseClock"):
        assert getattr(crawl, name) is getattr(process, name), name
    assert crawl.utc_stamp is helpers.utc_stamp
    assert crawl.XhsRuntimeSupervisionError is contracts.XhsRuntimeSupervisionError


# ---------- worker 输入与 fork config 写入 ----------

def test_str2bool_input_set_is_unchanged() -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    for text in CONFIG["__str2bool__"]["true"]:
        assert worker_inputs.str2bool(text) is True, text
    for text in CONFIG["__str2bool__"]["false"]:
        assert worker_inputs.str2bool(text) is False, text
    assert worker_inputs.str2bool(True) is True
    with pytest.raises(argparse.ArgumentTypeError):
        worker_inputs.str2bool("maybe")


@pytest.mark.parametrize("name", SCENARIOS)
def test_parent_generated_argv_parses(name: str, tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    inputs = worker_inputs.parse_cmd(_old_argv(name, tmp_path))
    assert inputs.platform == COMMANDS[name]["cmd"][COMMANDS[name]["cmd"].index("--platform") + 1]


@pytest.mark.parametrize(
    "mutation",
    [
        ("--platform", "bili"), ("--platform", "ks"), ("--platform", "tieba"),
        ("--save_data_option", "sqlite"), ("--headless", "maybe"), ("--lt", "password"),
    ],
)
def test_worker_inputs_reject_values_the_parent_never_generates(mutation: tuple[str, str], tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    argv = _old_argv("weibo_search", tmp_path)
    argv[argv.index(mutation[0]) + 1] = mutation[1]
    with pytest.raises(SystemExit) as raised:
        worker_inputs.parse_cmd(argv)
    assert raised.value.code == 2


@pytest.mark.parametrize("extra", [["--init_db", "sqlite"], ["--creator_id", "1"], ["--unknown", "x"]])
def test_worker_inputs_reject_unused_upstream_flags(extra: list[str], tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    with pytest.raises(SystemExit) as raised:
        worker_inputs.parse_cmd([*_old_argv("weibo_search", tmp_path), *extra])
    assert raised.value.code == 2


# T12：configure 写入根配置对象而非 fork config。上游退出平台、creator、评论计数与代理供应商键不再进入
# 配置对象；四站指定 ID 不迁上游示例，未由父侧传入时为空列表（规格 C0/D1，决策见 T12 审查包）。
T12_DROPPED_PREFIXES = ("BILI_", "KS_", "TIEBA_", "IP_PROXY_", "STATIC_PROXY_")
T12_DROPPED_KEYS = {
    "DY_CREATOR_ID_LIST", "WEIBO_CREATOR_ID_LIST", "XHS_CREATOR_ID_LIST",
    "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES", "CRAWLER_MAX_NOTES_COUNT",
}
SPECIFIED_ID_KEYS = {"dy": "DY_SPECIFIED_ID_LIST", "wb": "WEIBO_SPECIFIED_ID_LIST",
                     "zhihu": "ZHIHU_SPECIFIED_ID_LIST", "xhs": "XHS_SPECIFIED_NOTE_URL_LIST"}


@pytest.mark.parametrize("name", SCENARIOS)
def test_configure_writes_same_fork_config_as_old_parse_cmd(name: str, tmp_path: Path) -> None:
    written = CONFIG[name]["written"]
    argv = _old_argv(name, tmp_path)
    expected = {key: value for key, value in written.items()
                if not key.startswith(T12_DROPPED_PREFIXES) and key not in T12_DROPPED_KEYS}
    selected = argv[argv.index("--platform") + 1]
    for code, key in SPECIFIED_ID_KEYS.items():
        if not (code == selected and "--specified_id" in argv):
            expected[key] = []
    code = (
        "import json\n"
        f"from {ENTRY_MODULE} import configure, current_config\n"
        f"configure({argv!r})\n"
        "config = current_config()\n"
        f"print(json.dumps({{key: getattr(config, key) for key in {sorted(written)!r} if hasattr(config, key)}},"
        " ensure_ascii=False))\n"
    )
    actual = _json_tail(_run_python(code, cwd=tmp_path))
    actual = json.loads(json.dumps(actual, ensure_ascii=False).replace(str(tmp_path), "<TMP>"))
    assert actual == expected


# ---------- 选站延迟装配（F10） ----------

@pytest.mark.parametrize("code", ["wb", "zhihu", "xhs"])
def test_selected_platform_is_the_only_fork_platform_loaded(code: str, tmp_path: Path) -> None:
    scenario = {"wb": "weibo_search", "zhihu": "zhihu_search", "xhs": "xhs_search_qrcode"}[code]
    probe = (
        "import json, sys\n"
        f"from {ENTRY_MODULE} import configure, load_crawler\n"
        f"inputs = configure({_old_argv(scenario, tmp_path)!r})\n"
        "crawler_class = load_crawler(inputs.platform)\n"
        "platforms = sorted({name.split('.')[1] for name in sys.modules\n"
        "                    if name.startswith('media_platform.') and name.count('.') >= 1})\n"
        "print(json.dumps({'class': crawler_class.__name__, 'platforms': platforms,\n"
        "                  'execjs': 'execjs' in sys.modules}))\n"
    )
    report = _json_tail(_run_python(probe, cwd=tmp_path))
    assert report["platforms"] == []  # T09 起小红书也不再装载 fork 平台包
    # 知乎签名 JS 只在首次签名时编译；微博/小红书不装载 execjs
    if code != "zhihu":
        assert report["execjs"] is False


def test_configure_does_not_import_any_platform(tmp_path: Path) -> None:
    probe = (
        "import json, sys\n"
        f"from {ENTRY_MODULE} import configure\n"
        f"configure({_old_argv('douyin_search_discovery', tmp_path)!r})\n"
        "print(json.dumps(sorted(n for n in sys.modules if n.startswith(('media_platform', 'execjs', 'playwright')))))\n"
    )
    assert _json_tail(_run_python(probe, cwd=tmp_path)) == []


@pytest.mark.installation
def test_all_worker_platforms_import_in_root_environment_without_exit_slices(tmp_path: Path) -> None:
    # 抖音在导入时编译 libs/douyin.js，需要 Node；以任意 cwd 验证不依赖 fork 目录
    probe = (
        "import importlib, json\n"
        f"from {ENTRY_MODULE} import configure, load_crawler\n"
        f"configure({_old_argv('weibo_search', tmp_path)!r})\n"
        f"print(json.dumps([load_crawler(code).__name__ for code in {sorted(WORKER_PLATFORMS)!r}]))\n"
    )
    names = _json_tail(_run_python(probe, cwd=tmp_path, timeout=180))
    assert names == ["DouYinCrawler", "WeiboCrawler", "XiaoHongShuCrawler", "ZhihuCrawler"]


def test_entry_main_startup_and_cleanup_trace(tmp_path: Path) -> None:
    # 产物：选站启动/清理顺序 trace；与原 E.main→M.main→app_runner 顺序一致
    # T14：子进程不继承 conftest 的进程内重定向；probe 内把登录资料两个根改到 tmp，未迁移的 checkout 不影响本用例。
    probe = (
        "import json\n"
        + child_redirect_source(tmp_path / "platform_sessions_isolation") +
        f"import {ENTRY_MODULE} as entry\n"
        "from trippostcollect.runtime import worker\n"
        "trace = []\n"
        "def wrap(module, name):\n"
        "    original = getattr(module, name)\n"
        "    def wrapped(*args, **kwargs):\n"
        "        trace.append(name)\n"
        "        return original(*args, **kwargs)\n"
        "    setattr(module, name, wrapped)\n"
        "for name in ('configure', 'install_hooks'):\n"
        "    wrap(entry, name)\n"
        "class FakeCrawler:\n"
        "    browser_context = None\n"
        "    async def start(self):\n"
        "        trace.append('start')\n"
        "def fake_load(code):\n"
        "    trace.append('load:' + code)\n"
        "    return FakeCrawler\n"
        "entry.load_crawler = fake_load\n"
        "original_cleanup = worker.async_cleanup\n"
        "async def cleanup(crawler, platform):\n"
        "    trace.append('cleanup:' + platform)\n"
        "    await original_cleanup(crawler, platform)\n"
        "worker.async_cleanup = cleanup\n"
        f"code = entry.main({_old_argv('weibo_search', tmp_path)!r})\n"
        "print(json.dumps({'code': code, 'trace': trace}))\n"
    )
    report = _json_tail(_run_python(probe, cwd=tmp_path))
    assert report == {
        "code": 0,
        "trace": ["configure", "install_hooks", "load:wb", "start", "cleanup:wb"],
    }


def test_entry_module_is_runnable_with_isolated_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-P", "-m", ENTRY_MODULE, "--platform", "bili"],
        cwd=tmp_path, env=_child_environment(), capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 2, result.stderr[-2000:]


# ---------- 清理分派（原 M.async_cleanup） ----------

class _Recorder:
    def __init__(self, calls: list, name: str, error: Exception | None = None) -> None:
        self.calls, self.name, self.error = calls, name, error

    async def __call__(self, *args, **kwargs):
        self.calls.append((self.name, args, kwargs))
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_cleanup_closes_xhs_crawler_with_force() -> None:
    worker = import_module("trippostcollect.runtime.worker")
    calls: list = []
    crawler = type("C", (), {})()
    crawler.close = _Recorder(calls, "close")
    crawler.cdp_manager = type("M", (), {"cleanup": _Recorder(calls, "cdp")})()
    await worker.async_cleanup(crawler, "xhs")
    assert calls == [("close", (), {"force": True})]


@pytest.mark.asyncio
async def test_cleanup_prefers_cdp_manager_then_browser_context() -> None:
    worker = import_module("trippostcollect.runtime.worker")
    calls: list = []
    crawler = type("C", (), {})()
    crawler.cdp_manager = type("M", (), {"cleanup": _Recorder(calls, "cdp")})()
    crawler.browser_context = type("B", (), {"close": _Recorder(calls, "context")})()
    await worker.async_cleanup(crawler, "zhihu")
    assert calls == [("cdp", (), {"force": True})]
    calls.clear()
    crawler.cdp_manager = None
    await worker.async_cleanup(crawler, "wb")
    assert calls == [("context", (), {})]
    await worker.async_cleanup(None, "wb")


@pytest.mark.asyncio
@pytest.mark.parametrize(("message", "printed"), [("Target closed", False), ("disconnected", False), ("boom", True)])
async def test_cleanup_reports_only_unexpected_errors(
    message: str, printed: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    worker = import_module("trippostcollect.runtime.worker")
    crawler = type("C", (), {})()
    crawler.cdp_manager = None
    crawler.browser_context = type("B", (), {"close": _Recorder([], "context", RuntimeError(message))})()
    await worker.async_cleanup(crawler, "wb")
    output = capsys.readouterr().out
    assert ("[Main] Error closing browser context" in output) is printed


def test_run_invokes_cleanup_after_main_and_cancels_leftover_tasks() -> None:
    import asyncio

    worker = import_module("trippostcollect.runtime.worker")
    trace: list[str] = []

    async def main() -> None:
        async def leftover() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                trace.append("leftover-cancelled")
                raise

        asyncio.get_running_loop().create_task(leftover())
        await asyncio.sleep(0)
        trace.append("main")

    async def cleanup() -> None:
        trace.append("cleanup")

    worker.run(main, cleanup, cleanup_timeout_seconds=1.0)
    assert trace == ["main", "cleanup", "leftover-cancelled"]


def test_run_bounds_cleanup_time(capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    worker = import_module("trippostcollect.runtime.worker")

    async def main() -> None:
        return None

    async def slow_cleanup() -> None:
        await asyncio.sleep(5)

    worker.run(main, slow_cleanup, cleanup_timeout_seconds=0.05)
    assert "[Main] Cleanup timeout (0.05s)" in capsys.readouterr().out


# ---------- fork 不依赖 cwd ----------

FORK_LIVE_SOURCES = (
    "media_platform/weibo", "media_platform/douyin", "media_platform/zhihu", "media_platform/xhs",
    "tools", "store/weibo", "store/douyin", "store/zhihu", "store/xhs",
)


@expectations.legacy_only
def test_fork_live_sources_have_no_cwd_relative_paths() -> None:
    offenders = []
    for relative in FORK_LIVE_SOURCES:
        for path in sorted((FORK / relative).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in ("os.getcwd()", "Path.cwd()", "\"libs/", "'libs/", "Path(\"data\")", "\"data/{"):
                if needle in text:
                    offenders.append(f"{path.relative_to(FORK)}: {needle}")
    assert offenders == []


@expectations.legacy_only
@pytest.mark.installation
def test_fork_signature_js_loads_from_any_cwd(tmp_path: Path) -> None:
    # 真实编译包内 JS（需 Node），不发请求；两站签名均须在非 fork cwd 下得到非空结果
    probe = (
        "import json\n"
        # T12：新入口不再装载 fork；旧桥签名模块经过渡装载点显式加载。
        "from trippostcollect.platforms import _fork_bridge\n"
        "_fork_bridge.install()\n"
        "from media_platform.zhihu import help as zhihu_help\n"
        "from media_platform.douyin import help as douyin_help\n"
        "zhihu = zhihu_help.sign('/api/v4/search_v3?q=test', 'd_c0=AAAA')\n"
        "print(json.dumps({'zhihu': sorted(zhihu), 'douyin': bool(douyin_help.douyin_sign_obj)}))\n"
    )
    report = _json_tail(_run_python(probe, cwd=tmp_path, timeout=180))
    assert report["douyin"] is True
    assert report["zhihu"] == ["x-zse-96", "x-zst-81"]


# ---------- 迁移进度 ----------

# 旧桥在旧轮结束前保留（T14 删），浏览器/二维码运行时在 T02 第二个 PR 迁入
T02_PENDING_ALLOWED = {
    "tools/MediaCrawler/cmd_arg/arg.py",
    "tools/MediaCrawler/main.py",
    "scripts/mediacrawler_export_entrypoint.py",
    "tools/MediaCrawler/tools/browser_launcher.py",
    "tools/MediaCrawler/tools/cdp_browser.py",
    "tools/MediaCrawler/tools/crawler_util.py",
}


def test_t02_first_batch_definitions_are_moved() -> None:
    ledger = import_module("adapter_ledger")
    report = ledger.build_progress(ROOT)
    assert report["missing"] == []
    rows = [row for row in report["rows"] if row["card"] == "T02"]
    assert len(rows) == 81
    not_moved = sorted(
        (row["file"], row["qualname"]) for row in rows
        if row["state"] != "moved" and row["file"] not in T02_PENDING_ALLOWED
    )
    assert not_moved == []


def test_inputs_still_match_baseline() -> None:
    ledger = import_module("adapter_ledger")
    assert ledger.build_input_drift(ROOT) == {"cli_changed": {}, "env_added": [], "env_removed": []}
