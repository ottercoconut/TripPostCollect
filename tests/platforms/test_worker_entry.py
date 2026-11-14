"""worker 入口：父侧生成的子命令、worker 入参解析与配置写入、选站延迟装配、启动与清理顺序。"""

from __future__ import annotations

from trippostcollect.runtime.process import NO_PROGRESS_WATCHDOG_SECONDS

import argparse
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from support import worker_scenarios as scenarios
from support.isolated_python import child_environment, json_tail, run_python
from support.platform_sessions import child_redirect_source

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

ENTRY_MODULE = scenarios.ENTRY_MODULE
WORKER_PLATFORMS = {"wb": "weibo", "dy": "douyin", "zhihu": "zhihu", "xhs": "xhs"}


# ---------- 父侧构造的 child 命令 ----------

# 父层不再发出的 env：唯一消费者已删除（T12、T14-C、#124），小红书全量扫描不再注入发现恢复参数（#71）。
RETIRED_ENV = {
    "TRIPPOSTCOLLECT_DISCOVERY_RUN_ID", "TRIPPOSTCOLLECT_DISCOVERY_PLATFORM", "TRIPPOSTCOLLECT_DISCOVERY_KEYWORD",
    "TRIPPOSTCOLLECT_DISCOVERY_RESUME_PAGE", "TRIPPOSTCOLLECT_DISCOVERY_CHECKPOINT_WRITE_DISABLED",
    "TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_WAIT_SECONDS", "TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL",
    "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR",
}
RETIRED_XHS_ENV = {
    "TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES",
    "TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS",
}


def test_child_command_runs_root_entry_with_parent_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = scenarios.capture_parent_commands(monkeypatch, tmp_path)
    for name in scenarios.NAMES:
        command = captured[name]
        assert command["cmd"] == [sys.executable, "-P", "-m", ENTRY_MODULE,
                                  *scenarios.worker_argv(name, tmp_path)], name
        assert command["cwd"] == ROOT, name
        # #75：任务级时限删除，无进展看门狗阈值统一为代码常量。
        assert command["timeout"] == NO_PROGRESS_WATCHDOG_SECONDS, name
        env = command["extra_env"]
        assert env["TRIPPOSTCOLLECT_DB_PATH"] == str(tmp_path / "content.sqlite"), name
        # 续跑时已处理候选文件经 env 交给 worker。
        assert env.get("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH") == (
            str(tmp_path / "identities.json") if name == "weibo_search_resume" else None), name
        retired = RETIRED_ENV | (RETIRED_XHS_ENV if name.startswith("xhs_") else set())
        assert retired.isdisjoint(env), (name, sorted(retired & set(env)))
        assert all(key.startswith("TRIPPOSTCOLLECT_") for key in env), name


def test_child_environment_does_not_inject_uv_cache_or_pythonpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    process = import_module("trippostcollect.runtime.process")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    child = "import os, sys; sys.exit(int('UV_CACHE_DIR' in os.environ or 'PYTHONPATH' in os.environ))"
    result = process.run_command([sys.executable, "-c", child], tmp_path, 10, tmp_path / "logs", progress_paths=[])
    assert result["returncode"] == 0, result


# ---------- worker 入参与配置写入 ----------

def test_str2bool_input_set() -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    for text in ("yes", "true", "t", "y", "1", "YES", "True", "T", "Y"):
        assert worker_inputs.str2bool(text) is True, text
    for text in ("no", "false", "f", "n", "0", "NO", "False", "F", "N"):
        assert worker_inputs.str2bool(text) is False, text
    assert worker_inputs.str2bool(True) is True
    with pytest.raises(argparse.ArgumentTypeError):
        worker_inputs.str2bool("maybe")


@pytest.mark.parametrize("name", scenarios.NAMES)
def test_parent_generated_argv_parses(name: str, tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    inputs = worker_inputs.parse_cmd(scenarios.worker_argv(name, tmp_path))
    assert inputs.platform == scenarios.worker_code(name)


@pytest.mark.parametrize(
    "mutation",
    [
        ("--platform", "bili"), ("--platform", "ks"), ("--platform", "tieba"),
        ("--save_data_option", "sqlite"), ("--headless", "maybe"), ("--lt", "password"),
    ],
)
def test_worker_inputs_reject_values_the_parent_never_generates(mutation: tuple[str, str], tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    argv = scenarios.worker_argv("weibo_search", tmp_path)
    argv[argv.index(mutation[0]) + 1] = mutation[1]
    with pytest.raises(SystemExit) as raised:
        worker_inputs.parse_cmd(argv)
    assert raised.value.code == 2


@pytest.mark.parametrize("extra", [["--init_db", "sqlite"], ["--creator_id", "1"], ["--unknown", "x"]])
def test_worker_inputs_reject_unused_upstream_flags(extra: list[str], tmp_path: Path) -> None:
    worker_inputs = import_module("trippostcollect.application.worker_inputs")
    with pytest.raises(SystemExit) as raised:
        worker_inputs.parse_cmd([*scenarios.worker_argv("weibo_search", tmp_path), *extra])
    assert raised.value.code == 2


# configure 写入根配置对象。上游退出平台、creator、评论计数与代理供应商键不进入配置对象；
# 四站指定 ID 未由父侧传入时为空列表。
DROPPED_PREFIXES = ("BILI_", "KS_", "TIEBA_", "IP_PROXY_", "STATIC_PROXY_")
DROPPED_KEYS = {
    "DY_CREATOR_ID_LIST", "WEIBO_CREATOR_ID_LIST", "XHS_CREATOR_ID_LIST",
    "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES", "CRAWLER_MAX_NOTES_COUNT",
}
SPECIFIED_ID_KEYS = {"dy": "DY_SPECIFIED_ID_LIST", "wb": "WEIBO_SPECIFIED_ID_LIST",
                     "zhihu": "ZHIHU_SPECIFIED_ID_LIST", "xhs": "XHS_SPECIFIED_NOTE_URL_LIST"}


@pytest.mark.parametrize("name", scenarios.NAMES)
def test_configure_writes_worker_inputs_to_root_config(name: str, tmp_path: Path) -> None:
    argv = scenarios.worker_argv(name, tmp_path)
    _, _, code, login, crawler_type, headed, start, specified, cdp = scenarios.SCENARIOS[name]
    probe = (
        "import json\n"
        f"from {ENTRY_MODULE} import configure, current_config\n"
        f"configure({argv!r})\n"
        "config = current_config()\n"
        "print(json.dumps({key: getattr(config, key) for key in dir(config) if key.isupper()}, ensure_ascii=False))\n"
    )
    config = json_tail(run_python(probe, cwd=tmp_path))
    assert {key: config[key] for key in (
        "PLATFORM", "LOGIN_TYPE", "CRAWLER_TYPE", "START_PAGE", "KEYWORDS", "HEADLESS", "CDP_HEADLESS",
        "ENABLE_CDP_MODE", "SAVE_DATA_OPTION", "SAVE_DATA_PATH", "MAX_CONCURRENCY_NUM", "ENABLE_IP_PROXY",
        "ENABLE_GET_COMMENTS", "ENABLE_GET_SUB_COMMENTS", "ENABLE_GET_MEIDAS",
    )} == {
        "PLATFORM": code, "LOGIN_TYPE": login, "CRAWLER_TYPE": crawler_type, "START_PAGE": start,
        "KEYWORDS": scenarios.KEYWORD, "HEADLESS": not headed, "CDP_HEADLESS": not headed,
        "ENABLE_CDP_MODE": cdp == "true", "SAVE_DATA_OPTION": "jsonl",
        "SAVE_DATA_PATH": argv[argv.index("--save_data_path") + 1], "MAX_CONCURRENCY_NUM": 1,
        "ENABLE_IP_PROXY": False, "ENABLE_GET_COMMENTS": False, "ENABLE_GET_SUB_COMMENTS": False,
        "ENABLE_GET_MEIDAS": True,
    }
    for site, key in SPECIFIED_ID_KEYS.items():
        assert config[key] == ([specified] if specified and site == code else []), key
    assert not [key for key in config if key.startswith(DROPPED_PREFIXES) or key in DROPPED_KEYS]


# ---------- 选站延迟装配（F10） ----------

@pytest.mark.parametrize("code", ["wb", "zhihu", "xhs"])
def test_selected_platform_loads_no_upstream_platform_package(code: str, tmp_path: Path) -> None:
    scenario = {"wb": "weibo_search", "zhihu": "zhihu_search", "xhs": "xhs_search_qrcode"}[code]
    probe = (
        "import json, sys\n"
        f"from {ENTRY_MODULE} import configure, load_crawler\n"
        f"inputs = configure({scenarios.worker_argv(scenario, tmp_path)!r})\n"
        "crawler_class = load_crawler(inputs.platform)\n"
        "platforms = sorted({name.split('.')[1] for name in sys.modules\n"
        "                    if name.startswith('media_platform.') and name.count('.') >= 1})\n"
        "print(json.dumps({'class': crawler_class.__name__, 'platforms': platforms,\n"
        "                  'execjs': 'execjs' in sys.modules}))\n"
    )
    report = json_tail(run_python(probe, cwd=tmp_path))
    assert report["platforms"] == []
    # 知乎签名 JS 只在首次签名时编译；微博/小红书不装载 execjs
    if code != "zhihu":
        assert report["execjs"] is False


def test_configure_does_not_import_any_platform(tmp_path: Path) -> None:
    probe = (
        "import json, sys\n"
        f"from {ENTRY_MODULE} import configure\n"
        f"configure({scenarios.worker_argv('douyin_search_discovery', tmp_path)!r})\n"
        "print(json.dumps(sorted(n for n in sys.modules if n.startswith(('media_platform', 'execjs', 'playwright')))))\n"
    )
    assert json_tail(run_python(probe, cwd=tmp_path)) == []


@pytest.mark.installation
def test_all_worker_platforms_import_in_root_environment_without_exit_slices(tmp_path: Path) -> None:
    # 抖音在导入时编译 libs/douyin.js，需要 Node；以任意 cwd 验证不依赖仓库目录
    probe = (
        "import importlib, json\n"
        f"from {ENTRY_MODULE} import configure, load_crawler\n"
        f"configure({scenarios.worker_argv('weibo_search', tmp_path)!r})\n"
        f"print(json.dumps([load_crawler(code).__name__ for code in {sorted(WORKER_PLATFORMS)!r}]))\n"
    )
    names = json_tail(run_python(probe, cwd=tmp_path, timeout=180))
    assert names == ["DouYinCrawler", "WeiboCrawler", "XiaoHongShuCrawler", "ZhihuCrawler"]


def test_entry_main_startup_and_cleanup_trace(tmp_path: Path) -> None:
    # 选站启动/清理顺序：configure → install_hooks → 装载 → start → cleanup。
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
        f"code = entry.main({scenarios.worker_argv('weibo_search', tmp_path)!r})\n"
        "print(json.dumps({'code': code, 'trace': trace}))\n"
    )
    report = json_tail(run_python(probe, cwd=tmp_path))
    assert report == {
        "code": 0,
        "trace": ["configure", "install_hooks", "load:wb", "start", "cleanup:wb"],
    }


def test_entry_module_is_runnable_with_isolated_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-P", "-m", ENTRY_MODULE, "--platform", "bili"],
        cwd=tmp_path, env=child_environment(), capture_output=True, text=True, timeout=60, check=False,
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
